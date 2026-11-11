"""Discover perfume URLs with Patchright and maintain a resumable manifest."""

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from archiver.urls import canonical_url, read_manifest, too_small_to_replace

from archiver import browser_client, paths
from archiver.browser_client import (
    BASE_URL,
    FETCH_ERRORS,
    BrowserClient,
    CloudflareChallengeError,
    matching_urls,
)

OUTPUT_PATH = paths.manifest_file("perfumes_manifest.txt")
TEMP_OUTPUT_PATH = paths.manifest_file("perfumes_manifest.txt.tmp")
STATE_PATH = paths.manifest_file("perfumes_manifest.state.json")
EMPTY_PATH = paths.manifest_file("perfumes_manifest.empty.txt")  # designer pages that stayed empty after a retry
SHORT_PATH = paths.manifest_file("perfumes_manifest.short.txt")  # designers with fewer perfumes than last time: url, now, before
SHORT_SHARE = 0.9     # a designer with under 90% of the perfumes it had in the previous manifest ...
SHORT_MIN_GAP = 3     # ... and at least 3 fewer, is fetched once more, and recorded if still short
NEW_PATH = paths.manifest_file("perfumes_manifest.new.txt")      # a crawl too small to replace the manifest is kept here
LEGACY_TEMP_PATH = Path("manifest.txt.tmp")
LEGACY_OUTPUT_PATH = Path("manifest.txt")

COUNTRIES = [
    "United States", "France", "Italy", "United Kingdom", "United Arab Emirates", "Germany",
    "Brazil", "Russia", "Spain", "Netherlands", "Australia", "Turkey", "Japan", "South Korea",
    "Canada", "Sweden", "Switzerland", "Poland", "Romania", "Saudi Arabia", "China", "Portugal",
    "India", "Mexico", "Thailand",
]

BRAND_PATH = re.compile(r"/designers/[^/]+\.html$", re.IGNORECASE)
PERFUME_PATH = re.compile(r"/perfume/[^/]+/[^/]+-\d+\.html?$", re.IGNORECASE)
PERFUME_LINKS = 'a[href*="/perfume/"]'   # what the designer page scroll counts
BRAND_LINKS = 'a[href*="/designers/"]'


def migrate_legacy_manifest():
    """Preserve URL lists written by older versions using manifest.txt names."""
    if TEMP_OUTPUT_PATH.exists():
        return
    if LEGACY_TEMP_PATH.exists():
        LEGACY_TEMP_PATH.replace(TEMP_OUTPUT_PATH)
        print("Resuming from legacy temporary manifest: {}".format(TEMP_OUTPUT_PATH))
    elif LEGACY_OUTPUT_PATH.exists() and not OUTPUT_PATH.exists():
        LEGACY_OUTPUT_PATH.replace(TEMP_OUTPUT_PATH)
        print("Migrated legacy manifest to {}".format(TEMP_OUTPUT_PATH))


def load_progress():
    """Return the next country, the designer URL to resume at (or None), and URLs already saved.

    The position is stored as a designer URL, not an index: the designer list is read from a
    lazily loaded page and an index could point at a different designer on the next run."""
    migrate_legacy_manifest()
    seen = set()
    if TEMP_OUTPUT_PATH.exists():
        seen = set(read_manifest(TEMP_OUTPUT_PATH))

    if STATE_PATH.exists():
        with STATE_PATH.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
        country_index = int(state.get("country_index", 0))
        brand_url = state.get("brand_url")
        brand_index = int(state.get("brand_index", 0))   # checkpoints written by older versions
        if not 0 <= country_index <= len(COUNTRIES) or brand_index < 0:
            raise ValueError("Invalid crawl position in {}".format(STATE_PATH))
        if not TEMP_OUTPUT_PATH.exists() and (country_index or brand_url or brand_index):
            raise ValueError(
                "{} has a saved position but {} is missing; restoring the file or "
                "deleting {} to start over avoids silently skipping countries".format(
                    STATE_PATH, TEMP_OUTPUT_PATH, STATE_PATH)
            )
        return country_index, (brand_url if brand_url else brand_index or None), seen

    return 0, None, seen


def save_progress(country_index, brand_url=None):
    """Atomically save the next position: a country, and optionally a designer URL in it."""
    temporary_state = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    with temporary_state.open("w", encoding="utf-8") as state_file:
        json.dump({"country_index": country_index, "brand_url": brand_url}, state_file)
        state_file.flush()
        os.fsync(state_file.fileno())
    temporary_state.replace(STATE_PATH)


def start_position(brand_urls, resume, country):
    """Index in `brand_urls` to resume at: `resume` is a designer URL, or an old-style index."""
    if resume is None:
        return 0
    if isinstance(resume, int):
        if resume > len(brand_urls):
            raise ValueError("Checkpoint designer index {} exceeds {} designers for {}".format(
                resume, len(brand_urls), country))
        return resume
    if resume not in brand_urls:
        raise ValueError(
            "Checkpoint designer {} is not on the {} page any more; delete {} to restart "
            "that country".format(resume, country, STATE_PATH))
    return brand_urls.index(resume)


def brand_segment(perfume_url):
    """The designer part of a perfume URL: /perfume/<Brand>/<Name>-<id>.html -> <Brand>."""
    parts = urlsplit(perfume_url).path.split("/")
    return parts[2] if len(parts) > 3 and parts[1] == "perfume" else ""


def previous_counts(path=OUTPUT_PATH):
    """How many perfumes each designer had in the previous manifest (empty if there is none)."""
    if not path.exists():
        return Counter()
    with path.open(encoding="utf-8") as manifest:
        return Counter(brand_segment(line.strip()) for line in manifest if line.strip())


def expected_count(brand_url, found, counts):
    """Perfumes this designer had last time: by the designer page's name, else by the brand most
    of the perfumes found carry in their URLs (the two can differ)."""
    by_name = counts[Path(urlsplit(brand_url).path).stem]
    if by_name or not found:
        return by_name
    return counts[Counter(brand_segment(u) for u in found).most_common(1)[0][0]]


def is_short(found, expected):
    return expected - found >= SHORT_MIN_GAP and found < expected * SHORT_SHARE


def fetch_designer_perfumes(client, brand_url, counts=None):
    """(perfume URLs on a designer page, "ok" | "short" | "empty").

    A page with no perfumes, or with clearly fewer than the previous manifest had, is fetched once
    more, since a page that did not finish loading looks the same; the longer list wins. What is
    still empty or short after that is recorded and the crawl goes on."""
    counts = counts if counts is not None else Counter()
    best, expected = [], 0
    for attempt in (1, 2):
        tree = client.fetch_tree(brand_url, scroll_for=PERFUME_LINKS, allow_partial=True)
        urls = matching_urls(tree, brand_url, PERFUME_PATH)
        if len(urls) > len(best):
            best = urls
        if not getattr(client, "scroll_complete", True):
            # Still growing at the scroll limit: a second try would not get further.
            print("[Short] {} was still growing at the scroll limit; kept the {} perfumes found, "
                  "recorded in {}".format(brand_url, len(urls), SHORT_PATH), flush=True)
            with SHORT_PATH.open("a", encoding="utf-8") as short:
                short.write("{}\t{}\tscroll limit reached\n".format(brand_url, len(urls)))
            return urls, "short"
        expected = expected_count(brand_url, best, counts)
        if best and not is_short(len(best), expected):
            return best, "ok"
        if attempt == 1:
            print("[{}] {} had {} perfumes{}; trying once more".format(
                "Short" if best else "Empty", brand_url, len(best),
                " ({} last time)".format(expected) if best else ""), flush=True)
    if best:
        print("[Short] {} still has {} perfumes, {} last time; recorded in {}".format(
            brand_url, len(best), expected, SHORT_PATH), flush=True)
        with SHORT_PATH.open("a", encoding="utf-8") as short:
            short.write("{}\t{}\t{}\n".format(brand_url, len(best), expected))
        return best, "short"
    try:
        title = client.page.title().strip()
    except FETCH_ERRORS:
        title = "(title unavailable)"
    print("[Empty] {} still empty (title={!r}); recorded in {}".format(
        brand_url, title, EMPTY_PATH), flush=True)
    with EMPTY_PATH.open("a", encoding="utf-8") as empty:
        empty.write(brand_url + "\n")
    return [], "empty"


def write_canonical(path):
    """Rewrite a manifest so every URL has its canonical spelling and appears once (a resumed
    legacy file may hold http:// lines)."""
    urls = read_manifest(path)
    temporary = path.with_name(path.name + ".canonical")
    temporary.write_text("".join(url + "\n" for url in urls), encoding="utf-8")
    temporary.replace(path)


def print_summary(stats):
    """One line per country crawled in this run, then where the empty and short lists are."""
    if not stats:
        return
    print("\nSummary of this run:")
    print("{:<22}{:>10}{:>10}{:>8}{:>8}{:>8}".format("country", "designers", "perfumes", "new", "empty", "short"))
    for country, c in stats.items():
        print("{:<22}{:>10}{:>10}{:>8}{:>8}{:>8}".format(
            country, c["designers"], c["found"], c["new"], c["empty"], c["short"]))
    for label, path in (("empty", EMPTY_PATH), ("short", SHORT_PATH)):
        if path.exists() and path.read_text(encoding="utf-8").strip():
            print("Review the {} designers listed in {}.".format(label, path))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--country", choices=COUNTRIES, metavar="NAME",
        help="crawl only this country (for trial runs; use a scratch folder)",
    )
    parser.add_argument(
        "--designers", type=int, metavar="N",
        help="crawl only the first N designers of each country (for trial runs)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="replace {} even if the new list is under 90%% the size of the old one".format(OUTPUT_PATH))
    browser_client.add_browser_arguments(parser)
    args = parser.parse_args(argv)
    browser_client.apply_browser_arguments(args)
    countries = [args.country] if args.country else COUNTRIES
    paths.ensure_parent(OUTPUT_PATH)

    try:
        country_index, resume, seen = load_progress()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("Could not read crawl checkpoint: {}".format(exc))
        return 1

    counts = previous_counts()
    stats = {}      # country -> Counter, for the summary at the end

    print(
        "Starting at country {}/{}; {} unique URLs already saved.".format(
            min(country_index + 1, len(countries)), len(countries), len(seen)
        )
    )

    try:
        with BrowserClient() as client:
            with TEMP_OUTPUT_PATH.open("a", encoding="utf-8") as output:
                for current_country_index in range(country_index, len(countries)):
                    country = countries[current_country_index]
                    country_url = urljoin(BASE_URL, "/country/{}.html".format(country))
                    print("[Country] {}".format(country_url), flush=True)
                    country_tree = client.fetch_tree(country_url, scroll_for=BRAND_LINKS)
                    brand_urls = matching_urls(country_tree, country_url, BRAND_PATH)
                    if not brand_urls:
                        print("[Empty] {} listed no designers; trying once more".format(country_url), flush=True)
                        country_tree = client.fetch_tree(country_url, scroll_for=BRAND_LINKS)
                        brand_urls = matching_urls(country_tree, country_url, BRAND_PATH)
                    if not brand_urls:
                        # Carrying on would skip the whole country and advance the checkpoint past it.
                        raise ValueError("the {} page listed no designers, even after a second try".format(country))
                    if args.designers is not None:
                        brand_urls = brand_urls[:args.designers]

                    start = start_position(
                        brand_urls, resume if current_country_index == country_index else None, country
                    )
                    for current_brand_index in range(start, len(brand_urls)):
                        brand_url = brand_urls[current_brand_index]
                        print("[Designer {}/{}] {}".format(
                            current_brand_index + 1, len(brand_urls), brand_url
                        ), flush=True)

                        added = 0
                        found, status = fetch_designer_perfumes(client, brand_url, counts)
                        for perfume_url in found:
                            if perfume_url in seen:
                                continue
                            seen.add(perfume_url)
                            output.write(perfume_url + "\n")
                            added += 1

                        # URLs reach the disk before the position that says they are done.
                        output.flush()
                        os.fsync(output.fileno())
                        if current_brand_index + 1 < len(brand_urls):
                            save_progress(current_country_index, brand_urls[current_brand_index + 1])
                        else:
                            save_progress(current_country_index + 1)

                        tally = stats.setdefault(country, Counter())
                        tally["designers"] += 1
                        tally["found"] += len(found)
                        tally["new"] += added
                        tally["empty"] += status == "empty"
                        tally["short"] += status == "short"

                        print("[Saved] {} perfumes found, {} new; {} total unique URLs.".format(
                            len(found), added, len(seen)
                        ), flush=True)

                    save_progress(current_country_index + 1)

        if not seen:
            print("No perfume URLs were found; the existing manifest was not replaced.")
            return 1

        old_total = len(read_manifest(OUTPUT_PATH))
        if too_small_to_replace(len(seen), old_total, args.force):
            # A trial run (--country, --designers) or a crawl that missed part of the site.
            TEMP_OUTPUT_PATH.replace(NEW_PATH)
            STATE_PATH.unlink(missing_ok=True)
            print("NOT replaced: {} found {} URLs but the existing {} has {}. The new list is in {}; "
                  "if it is right, rename it over {} or rerun with --force.".format(
                      "this crawl", len(seen), OUTPUT_PATH, old_total, NEW_PATH, OUTPUT_PATH))
            return 1
        write_canonical(TEMP_OUTPUT_PATH)
        TEMP_OUTPUT_PATH.replace(OUTPUT_PATH)
        STATE_PATH.unlink(missing_ok=True)
        print("Completed: saved {} unique perfume URLs to {}.".format(len(seen), OUTPUT_PATH))
        return 0

    except CloudflareChallengeError as exc:
        print("Cloudflare challenge detected; crawl paused without advancing the checkpoint.")
        print("{}".format(exc))
        print("Progress remains in {} and {}. Resume later when normal access is available.".format(
            TEMP_OUTPUT_PATH, STATE_PATH
        ))
        return 1
    except (*FETCH_ERRORS, ValueError) as exc:
        print(
            "Crawl stopped: {}. Progress remains in {} and {}. "
            "Wait before rerunning; the crawler will resume from its checkpoint.".format(
                exc, TEMP_OUTPUT_PATH, STATE_PATH
            )
        )
        return 1
    finally:
        print_summary(stats)


if __name__ == "__main__":
    raise SystemExit(main())
