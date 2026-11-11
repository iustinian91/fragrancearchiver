"""Sanity-check a finished perfume crawl.

Usage:
    python verify_scrape.py [--db perfumes.sqlite] [--old old.sqlite] [--images fragrantica_images/perfumes]

Exit status is 1 if a hard problem is found (missing/duplicate pages), 0 otherwise.
Coverage lines are warnings: a field near 0% usually means the site layout changed.
"""

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from archiver import paths
from archiver.urls import canonical_url, read_manifest

PAGE_ID = re.compile(r"-(\d+)\.html?$")
IMAGE_ID = re.compile(r"^o\.(\d+)\.")
FIELDS = ("launch_year", "main_accords", "notes", "longevity", "sillage", "gender", "when_to_wear", "perfumers")


COLUMNS = ("brand", "perfume", "launch_year", "main_accords", "notes", "longevity", "sillage",
           "gender", "when_to_wear", "perfumers", "url", "page_id")


def load(db):
    """All rows as tuples in COLUMNS order (columns an older database lacks read as None)."""
    conn = sqlite3.connect("file:{}?mode=ro".format(db), uri=True)
    have = {row[1] for row in conn.execute("PRAGMA table_info(perfumes)")}
    select = ", ".join(c if c in have else "NULL" for c in COLUMNS)
    rows = conn.execute("SELECT {} FROM perfumes".format(select)).fetchall()
    try:
        progress = {r[0] for r in conn.execute("SELECT url FROM scrape_progress")}
    except sqlite3.OperationalError:
        progress = None
    conn.close()
    return rows, progress


def skipped_counts(db):
    """(pages given up after repeated failures, pages the site no longer has); (0, 0) for a database
    that has no such tables."""
    conn = sqlite3.connect("file:{}?mode=ro".format(db), uri=True)
    try:
        failed = conn.execute("SELECT COUNT(*) FROM skip_attempts").fetchone()[0]
    except sqlite3.OperationalError:
        failed = 0
    try:
        gone = conn.execute("SELECT COUNT(*) FROM gone_urls").fetchone()[0]
    except sqlite3.OperationalError:
        gone = 0
    conn.close()
    return failed, gone


def zero_vote(value):
    """True for a stored vote chart whose counts are all zero (nobody has voted)."""
    try:
        data = json.loads(value or "null")
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and bool(data) and all(str(v).isdigit() and not int(v) for v in data.values())


def info(row):
    try:
        data = json.loads(row[1])
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def present(value):
    """True for a stored value that carries data; a vote chart whose counts are all zero does not."""
    if value in (None, "", "null", "[]", "{}"):
        return False
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return True
    if isinstance(data, dict) and data and all(isinstance(v, (int, str)) and str(v).isdigit() for v in data.values()):
        return any(int(v) for v in data.values())
    return True


def page_id(row):
    """The page's id: stored with the row, else the one in its bottle image's file name."""
    if row[11]:
        return str(row[11])
    match = IMAGE_ID.match(info(row).get("image") or "")
    return match.group(1) if match else None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(paths.database_file("perfumes.sqlite")))
    ap.add_argument("--manifest", default=str(paths.manifest_file("perfumes_manifest.txt")))
    ap.add_argument("--old", help="previous perfumes.sqlite to compare against")
    ap.add_argument("--images", default=str(paths.image_dir("perfumes")))
    args = ap.parse_args()

    rows, progress = load(args.db)
    urls = read_manifest(args.manifest)
    progress = None if progress is None else {canonical_url(u) for u in progress}
    problems = 0
    print("Rows: {}   Manifest URLs: {}".format(len(rows), len(urls)))

    # 1. Completeness: every manifest URL scraped, nothing scraped twice
    if progress is None:
        print("WARN no scrape_progress table; completeness checked by image id only")
    else:
        missing = [u for u in urls if u not in progress]
        extra = progress - set(urls)
        print("Manifest URLs not marked complete: {}   Completed URLs not in manifest: {}".format(
            len(missing), len(extra)))
        problems += bool(missing)
        if len(progress) != len(rows):
            print("PROBLEM {} rows but {} completed URLs".format(len(rows), len(progress)))
            problems += 1
    ids = Counter(page_id(r) for r in rows if page_id(r))
    dupes = sum(n - 1 for n in ids.values() if n > 1)
    no_id = sum(1 for r in rows if not page_id(r))
    url_ids = {m.group(1) for m in map(PAGE_ID.search, urls) if m}
    no_url = sum(1 for r in rows if not r[10])
    print("Duplicate pages: {}   Rows without a page id: {}   Rows without a URL: {}   Manifest ids with no row: {}".format(
        dupes, no_id, no_url, len(url_ids - set(ids))))
    problems += bool(dupes)

    # 2. Field coverage
    n = max(len(rows), 1)
    print("\nCoverage (rows with the field filled):")
    for i, field in enumerate(FIELDS):
        count = sum(1 for r in rows if present(r[2 + i]))
        print("  {:<13}{:>7} {:>6.1f}%".format(field, count, 100 * count / n))
    zero = {field: sum(1 for r in rows if zero_vote(r[2 + i]))
            for i, field in enumerate(FIELDS) if field in ("longevity", "sillage", "gender", "when_to_wear")}
    print("  charts nobody has voted on (stored as zeros, not counted above): " +
          ", ".join("{} {}".format(f, n) for f, n in zero.items()))
    failed, gone = skipped_counts(args.db)
    print("  pages still failing after retries: {}   pages gone from the site: {}".format(failed, gone))
    from_page = Counter((info(r).get("title_source"), info(r).get("brand_source")) for r in rows)
    print("  names page/page: {:.1f}%   (url fallbacks: {})".format(
        100 * from_page[("page", "page")] / n, len(rows) - from_page[("page", "page")]))
    empty = sum(1 for r in rows if not (info(r).get("title") or "").strip())
    print("  empty titles: {}".format(empty))

    # 3. Suspicious values
    odd = [r for r in rows if re.search(r"\b[A-Za-z] [Ss] \w", info(r).get("title") or "")
           and info(r).get("title_source") != "page"]
    print("  slug-style names like \"Victoria S Secret\" left unrepaired: {}".format(len(odd)))
    for r in odd[:5]:
        print("     {} / {}".format(r[0], info(r).get("title")))

    # 4. Images on disk
    folder = Path(args.images)
    if folder.is_dir():
        gone = sum(1 for r in rows if info(r).get("image") and not (folder / info(r)["image"]).exists())
        print("  rows whose image file is missing on disk: {}".format(gone))

    # 5. Compare with an older crawl of the same pages
    if args.old:
        old_rows, _ = load(args.old)
        old = {page_id(r): r for r in old_rows if page_id(r)}
        same_year = both = lost = 0
        for r in rows:
            o = old.get(page_id(r))
            if not o:
                continue
            both += 1
            same_year += o[2] == r[2]
            lost += sum(present(o[2 + i]) and not present(r[2 + i]) for i in range(len(FIELDS))) > 0
        print("\nVersus {}: {} pages in both".format(args.old, both))
        print("  same launch year: {:.1f}%".format(100 * same_year / max(both, 1)))
        print("  rows that LOST a field the old crawl had: {}  (an older crawl has no gender or when_to_wear, so those never count)".format(lost))

    print("\n{}".format("FAILED" if problems else "OK: no completeness problems"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
