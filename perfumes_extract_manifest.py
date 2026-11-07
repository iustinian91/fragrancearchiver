"""Discover perfume URLs with Patchright and maintain a resumable manifest."""

import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from browser_client import BASE_URL, FETCH_ERRORS, BrowserClient, CloudflareChallengeError

OUTPUT_PATH = Path("perfumes_manifest.txt")
TEMP_OUTPUT_PATH = Path("perfumes_manifest.txt.tmp")
STATE_PATH = Path("perfumes_manifest.state.json")
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


def matching_urls(tree, base_url, path_fragment, pattern):
    """Return unique absolute URLs matching a path pattern.

    Resolve relative hrefs before validating their paths. Fragrantica pages can
    expose root-relative, path-relative, or absolute links.
    """
    urls = []
    for href in tree.xpath("//a[@href]/@href"):
        absolute_url = urljoin(base_url, href)
        parts = urlsplit(absolute_url)
        if path_fragment not in parts.path:
            continue
        if pattern.fullmatch(parts.path):
            # Drop query strings/fragments so one page has one manifest URL.
            urls.append("{}://{}{}".format(parts.scheme, parts.netloc, parts.path))
    return list(dict.fromkeys(urls))


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
    """Return the next country/designer position and URLs already checkpointed."""
    migrate_legacy_manifest()
    seen = set()
    if TEMP_OUTPUT_PATH.exists():
        with TEMP_OUTPUT_PATH.open(encoding="utf-8") as manifest:
            seen = {line.strip() for line in manifest if line.strip()}

    if STATE_PATH.exists():
        with STATE_PATH.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
        country_index = int(state.get("country_index", 0))
        brand_index = int(state.get("brand_index", 0))
        if not 0 <= country_index <= len(COUNTRIES) or brand_index < 0:
            raise ValueError("Invalid crawl position in {}".format(STATE_PATH))
        return country_index, brand_index, seen

    return 0, 0, seen


def save_progress(country_index, brand_index):
    """Atomically save the next position to process."""
    temporary_state = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    with temporary_state.open("w", encoding="utf-8") as state_file:
        json.dump({"country_index": country_index, "brand_index": brand_index}, state_file)
        state_file.flush()
    temporary_state.replace(STATE_PATH)


def main():
    try:
        country_index, brand_index, seen = load_progress()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("Could not read crawl checkpoint: {}".format(exc))
        return 1

    print(
        "Starting at country {}/{}; {} unique URLs already saved.".format(
            min(country_index + 1, len(COUNTRIES)), len(COUNTRIES), len(seen)
        )
    )

    try:
        with BrowserClient() as client:
            with TEMP_OUTPUT_PATH.open("a", encoding="utf-8") as output:
                for current_country_index in range(country_index, len(COUNTRIES)):
                    country = COUNTRIES[current_country_index]
                    country_url = urljoin(BASE_URL, "/country/{}.html".format(country))
                    print("[Country] {}".format(country_url), flush=True)
                    country_tree = client.fetch_tree(country_url)

                    brand_urls = matching_urls(
                        country_tree, country_url, "/designers/", BRAND_PATH
                    )

                    start_brand = brand_index if current_country_index == country_index else 0
                    if start_brand > len(brand_urls):
                        raise ValueError(
                            "Checkpoint designer index {} exceeds {} designers for {}".format(
                                start_brand, len(brand_urls), country
                            )
                        )

                    for current_brand_index in range(start_brand, len(brand_urls)):
                        brand_url = brand_urls[current_brand_index]
                        print("[Designer {}/{}] {}".format(
                            current_brand_index + 1, len(brand_urls), brand_url
                        ), flush=True)
                        brand_tree = client.fetch_tree(brand_url)

                        raw_perfume_hrefs = [
                            href for href in brand_tree.xpath("//a[@href]/@href")
                            if "/perfume/" in urlsplit(urljoin(brand_url, href)).path
                        ]
                        perfume_urls = matching_urls(
                            brand_tree, brand_url, "/perfume/", PERFUME_PATH
                        )

                        if not perfume_urls:
                            try:
                                page_title = client.page.title().strip()
                            except Exception:
                                page_title = "(title unavailable)"
                            print(
                                "[No matches] {} | title={!r} | raw perfume links={} | "
                                "examples={!r}".format(
                                    brand_url,
                                    page_title,
                                    len(raw_perfume_hrefs),
                                    raw_perfume_hrefs[:5],
                                ),
                                flush=True,
                            )

                        added = 0
                        for perfume_url in perfume_urls:
                            if perfume_url in seen:
                                continue
                            seen.add(perfume_url)
                            output.write(perfume_url + "\n")
                            added += 1

                        output.flush()
                        if current_brand_index + 1 < len(brand_urls):
                            save_progress(current_country_index, current_brand_index + 1)
                        else:
                            save_progress(current_country_index + 1, 0)

                        print("[Saved] {} new URLs; {} total unique URLs.".format(
                            added, len(seen)
                        ), flush=True)

                    save_progress(current_country_index + 1, 0)
                    brand_index = 0

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
