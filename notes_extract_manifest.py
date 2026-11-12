"""Extract the Fragrantica note manifest and replace it atomically."""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from archiver.urls import read_manifest, too_small_to_replace

from archiver import browser_client, paths
from archiver.browser_client import BASE_URL, FETCH_ERRORS, SITE_HOSTS, BrowserClient, matching_urls

OUTPUT_PATH = paths.manifest_file("notes_manifest.txt")
TEMP_OUTPUT_PATH = paths.manifest_file("notes_manifest.txt.tmp")
NEW_PATH = paths.manifest_file("notes_manifest.new.txt")   # a list too small to replace the manifest is kept here
CATEGORIES_PATH = paths.manifest_file("notes_categories.json")        # {note url: category}, saved with the manifest
NEW_CATEGORIES_PATH = paths.manifest_file("notes_categories.new.json")
NOTE_PATH = re.compile(r"/notes/[^/]+-\d+\.html?$", re.IGNORECASE)


def note_categories(tree, notes_url):
    """{note url: category}. The notes page lists the notes under uppercase headings (CITRUS SMELLS,
    FLOWERS, SPICES, ...), so a note belongs to the last such heading before its link."""
    categories, current = {}, None
    for element in tree.iter("h2", "a"):
        if element.tag == "h2":
            text = " ".join(element.text_content().split())
            if text and text.isupper():
                current = text
            continue
        href = element.get("href")
        if not href or current is None:
            continue
        parts = urlsplit(urljoin(notes_url, href))
        if parts.netloc.lower() in SITE_HOSTS and NOTE_PATH.fullmatch(parts.path):
            categories.setdefault(urljoin(BASE_URL, parts.path), current)
    return categories


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="replace {} even if the new list is under 90%% the size of the old one".format(OUTPUT_PATH))
    browser_client.add_browser_arguments(parser)
    args = parser.parse_args(argv)
    browser_client.apply_browser_arguments(args)
    paths.ensure_parent(OUTPUT_PATH)
    # Fetch and validate the page before touching the existing manifest.
    try:
        with BrowserClient() as client:
            tree = client.fetch_tree(urljoin(BASE_URL, "/notes/"))
    except FETCH_ERRORS as exc:
        print("Could not fetch the notes page; {} was not changed: {}".format(
            OUTPUT_PATH, exc
        ))
        return 1

    # Every link to a note page (/notes/<Name>-<id>.html), wherever the page puts them; the
    # menu's plain /notes/ link does not match.
    notes_url = urljoin(BASE_URL, "/notes/")
    note_urls = matching_urls(tree, notes_url, NOTE_PATH)
    if not note_urls:
        print("No note URLs were found; {} was not changed.".format(OUTPUT_PATH))
        return 1

    categories = note_categories(tree, notes_url)
    old_total = len(read_manifest(OUTPUT_PATH))
    target = OUTPUT_PATH
    if too_small_to_replace(len(note_urls), old_total, args.force):
        target = NEW_PATH
        print("NOT replaced: {} note URLs found but the existing {} has {}. The new list is in {}; "
              "if it is right, rename it over {} or rerun with --force.".format(
                  len(note_urls), OUTPUT_PATH, old_total, NEW_PATH, OUTPUT_PATH))
    try:
        with TEMP_OUTPUT_PATH.open("w", encoding="utf-8") as output:
            for url in note_urls:
                output.write(url + "\n")
            output.flush()
        TEMP_OUTPUT_PATH.replace(target)
        categories_target = CATEGORIES_PATH if target is OUTPUT_PATH else NEW_CATEGORIES_PATH
        categories_tmp = categories_target.with_name(categories_target.name + ".tmp")
        categories_tmp.write_text(json.dumps(categories, ensure_ascii=False, indent=0), encoding="utf-8")
        categories_tmp.replace(categories_target)
    except OSError as exc:
        print("Could not save {}: {}".format(OUTPUT_PATH, exc))
        return 1

    if target is NEW_PATH:
        return 1
    print("Saved {} unique note URLs to {}.".format(len(note_urls), OUTPUT_PATH))
    names = sorted(set(categories.values()))
    print("Categories: {} notes in {} categories saved to {}.".format(len(categories), len(names), CATEGORIES_PATH))
    if len(categories) < len(note_urls):
        print("WARNING: {} notes have no category on the page.".format(len(note_urls) - len(categories)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
