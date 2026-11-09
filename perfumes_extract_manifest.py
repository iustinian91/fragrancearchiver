"""Discover perfume URLs with Patchright and maintain a resumable manifest."""

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin

from browser_client import (
    BASE_URL,
    FETCH_ERRORS,
    BrowserClient,
    CloudflareChallengeError,
    matching_urls,
)

OUTPUT_PATH = Path("perfumes_manifest.txt")
TEMP_OUTPUT_PATH = Path("perfumes_manifest.txt.tmp")
STATE_PATH = Path("perfumes_manifest.state.json")
EMPTY_PATH = Path("perfumes_manifest.empty.txt")  # designer pages that stayed empty after a retry
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
        with TEMP_OUTPUT_PATH.open(encoding="utf-8") as manifest:
            seen = {line.strip() for line in manifest if line.strip()}

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


def fetch_designer_perfumes(client, brand_url):
    """Perfume URLs on a designer page. An empty page is fetched once more before it is
    accepted as empty, since a page that did not finish loading looks the same."""
    for attempt in (1, 2):
        tree = client.fetch_tree(brand_url, scroll_for=PERFUME_LINKS)
        urls = matching_urls(tree, brand_url, PERFUME_PATH)
        if urls:
            return urls
        if attempt == 1:
            print("[Empty] {} had no perfumes; trying once more".format(brand_url), flush=True)
    try:
        title = client.page.title().strip()
    except FETCH_ERRORS:
        title = "(title unavailable)"
    print("[Empty] {} still empty (title={!r}); recorded in {}".format(
        brand_url, title, EMPTY_PATH), flush=True)
    with EMPTY_PATH.open("a", encoding="utf-8") as empty:
        empty.write(brand_url + "\n")
    return []


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
    args = parser.parse_args(argv)
    countries = [args.country] if args.country else COUNTRIES

    try:
        country_index, resume, seen = load_progress()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("Could not read crawl checkpoint: {}".format(exc))
        return 1

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
                        found = fetch_designer_perfumes(client, brand_url)
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

                        print("[Saved] {} perfumes found, {} new; {} total unique URLs.".format(
                            len(found), added, len(seen)
                        ), flush=True)

                    save_progress(current_country_index + 1)

        if not seen:
            print("No perfume URLs were found; the existing manifest was not replaced.")
            return 1

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


if __name__ == "__main__":
    raise SystemExit(main())
