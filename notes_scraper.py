"""Scrape note pages and download their images using Patchright."""

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import browser_client
from browser_client import FETCH_ERRORS, BrowserClient
from db_helper import NotesDBHelper

IMAGE_DIR = Path("fragrantica_images/notes")
# The note's own photos: .../mdimg/sastojci/t.75.jpg, t2.75.jpg, t3.75.jpg (up to three per note).
# The page also shows many other pictures (thumbnails of perfumes), which must not match.
NOTE_PHOTO = re.compile(r"/mdimg/sastojci/[a-z]\d*\.\d+\.jpe?g$", re.IGNORECASE)


def note_photo_urls(tree, page_url):
    """Absolute URLs of the note's photos, in page order, without repeats."""
    sources = tree.xpath("//img/@src")
    if not any(NOTE_PHOTO.search(urlsplit(src).path) for src in sources):
        sources = tree.xpath("//h3/following-sibling::div/div/img/@src")  # layout used until 2022
        return list(dict.fromkeys(urljoin(page_url, src) for src in sources))
    return list(dict.fromkeys(
        urljoin(page_url, src) for src in sources if NOTE_PHOTO.search(urlsplit(src).path)
    ))


def scrape_note(client, url):
    """Fetch one note page and save its images. Returns (title, image file names)."""
    tree = client.fetch_tree(url)
    titles = [" ".join(t.split()) for t in tree.xpath("//h1/text()") if t.strip()]
    if not titles:
        raise ValueError("No title found on {}".format(url))

    note_title = titles[0]
    note_image_file_names = []
    for image_url in note_photo_urls(tree, url):
        # The file name is the URL's path; the query string is only a cache-buster.
        image_file_name = urlsplit(image_url).path.rsplit("/", 1)[-1]
        if client.download_image(image_url, IMAGE_DIR / image_file_name) is False:
            continue  # the site no longer has this picture; don't list a file we don't have
        note_image_file_names.append(image_file_name)

    return note_title, note_image_file_names


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default="notes.sqlite", help="SQLite file to write (default: %(default)s)"
    )
    parser.add_argument(
        "--limit", type=int, help="scrape at most this many pages (for trial runs)"
    )
    browser_client.add_browser_arguments(parser)
    args = parser.parse_args(argv)
    browser_client.apply_browser_arguments(args)

    db = NotesDBHelper(args.db)
    db.setup()
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with open("notes_manifest.txt", encoding="utf-8") as manifest:
            note_urls = list(dict.fromkeys(
                line.strip() for line in manifest if line.strip()
            ))
        db.migrate_legacy_progress(note_urls)
        completed = db.completed_urls()
    except (OSError, ValueError, RuntimeError) as exc:
        print("Could not prepare notes scrape: {}".format(exc))
        db.close()
        return 1

    total = len(note_urls)
    pending = [url for url in note_urls if url not in completed]
    if args.limit is not None:
        pending = pending[:args.limit]
    print(
        "Notes scrape: {} URLs in manifest, {} already complete, {} remaining.".format(
            total, len(completed), len(pending)
        ),
        flush=True,
    )

    try:
        with BrowserClient() as client:
            for position, url in enumerate(pending, start=1):
                note_title, image_names = scrape_note(client, url)
                db.add_record_for_url(url, (note_title, json.dumps(image_names)))
                completed.add(url)
                print({
                    "progress": "{}/{}".format(position, len(pending)),
                    "completed_total": len(completed),
                    "note": note_title,
                }, flush=True)
    except (*FETCH_ERRORS, ValueError) as exc:
        print(
            "Scrape paused: {}. {} URLs are recorded as complete; rerun to continue "
            "with the remaining URLs.".format(exc, len(completed)),
            flush=True,
        )
        return 1
    finally:
        db.close()

    print("Completed: {} note URLs processed.".format(len(completed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
