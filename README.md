# fragrancearchiver

A Python project for collecting fragrance specifications.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
patchright install chromium
```

The four crawling scripts use a shared visible Chromium browser through
Patchright. They use ordinary browser navigation and page rendering, with no
stealth settings, CAPTCHA solvers, proxy rotation, or automated challenge
bypass. Requests are sequential and paced by randomized delays in
`browser_client.py`.

HTTP 429 responses are retried for the same URL after the server's
`Retry-After` interval plus a two-second buffer. If that header is missing or
invalid, the client waits five minutes plus the buffer. Other HTTP errors and
Cloudflare challenges stop the current crawl without marking the URL complete.

## Where files live

Code is in the project folder (`archiver/` holds the shared helpers); everything the commands
generate goes under one folder, `data/` (set `ARCHIVER_DATA_DIR` to put it elsewhere):

```
data/
  manifests/        perfumes_manifest.txt, notes_manifest.txt and their working files
  databases/        perfumes.sqlite, notes.sqlite, app.db
  exports/          perfumes.json, perfumes_clean.json (rebuildable)
  images/           perfumes/, notes/
  logs/             skipped, gone and failed page lists, and the skipped pages themselves
  browser_profile/  the browser's cookies, kept between runs
  dry_runs/         one folder per dry test run
```

Names in the rest of this file are shortened (`perfumes.sqlite` means `data/databases/perfumes.sqlite`).
Coming from an older checkout with the files in the project folder? Run `python migrate_layout.py`
to see what would move, then `python migrate_layout.py --apply`. Nothing is overwritten.

## Perfume URL discovery

Run:

```bash
python perfumes_extract_manifest.py
```

The browser opens visibly. The crawler visits country and designer pages. It
writes discovered URLs incrementally to `data/manifests/perfumes_manifest.txt.tmp` and saves
its next country/designer position in `data/manifests/perfumes_manifest.state.json`.

Country and designer pages load their lists as you scroll, so the crawler scrolls each
one until the number of links stops growing. If a page is still growing after 150 scroll
steps it stops instead of saving a partial list. A designer page with no perfumes is
fetched once more; if it is still empty it is noted in `data/manifests/perfumes_manifest.empty.txt` and
the crawl continues. Only links on fragrantica.com are kept, written as
`https://www.fragrantica.com/...` without query strings, so each perfume has one URL.

The checkpoint stores the next designer's URL (not a position in a list), so it still
lands in the right place if the page lists designers in a slightly different order next
time. If the saved designer is no longer listed, or the checkpoint exists without
`data/manifests/perfumes_manifest.txt.tmp`, the crawler stops and says so instead of guessing.

If an HTTP error or a Cloudflare challenge is detected, the crawl stops and
preserves the manifest and checkpoint. If a challenge page appears, handle
access through the browser/site's normal supported process; the crawler does
not attempt to bypass it. Rerun the script to resume from its saved position.

Do not delete the temporary manifest or state file if you want to resume. Once
all countries are processed, the temporary manifest is renamed to
`perfumes_manifest.txt`.

The crawler also migrates legacy `manifest.txt.tmp` or `manifest.txt` files
to the new `data/manifests/perfumes_manifest.txt.tmp` checkpoint name when appropriate.
If a legacy manifest exists without a checkpoint, its URLs are retained and
discovery safely restarts from the first country, skipping duplicates.

## Notes manifest

Run:

```bash
python notes_extract_manifest.py
```

The notes index is a single-page discovery step, so it has no per-page crawl
checkpoint. It deduplicates the extracted URLs and writes to
`notes_manifest.txt.tmp` first, then atomically replaces `notes_manifest.txt`
only after successful extraction. A failed request or write leaves the
previous completed manifest untouched. Rerunning repeats this one-page
discovery operation.

## Scraping and URL-based resume

Run:

```bash
python perfumes_scraper.py
python notes_scraper.py
```

Each scraper records completed source URLs in a `scrape_progress` table in its
own SQLite database (`perfumes.sqlite` or `notes.sqlite`). A scraped data row
and its URL completion marker are committed in one transaction. On restart,
the scraper reads the completion ledger and processes only URLs not yet
recorded as complete. It can therefore resume even if manifest entries are
reordered or new URLs are added.

Images are downloaded through the same browser context and stored in
`data/images/perfumes/` and `data/images/notes/`.

### Existing database migration

Older database files do not have URL completion records. On the first run with
this version, the scraper seeds the progress table from the first N unique URLs
in the current manifest, where N is the existing number of database records.
This preserves the old sequential-resume behavior **only if the manifest is
still in the same order and contains the original entries**. Verify that before
the first upgraded run. If the manifest was reordered, edited, or shortened,
do not let the automatic migration run: back up the database and reconcile
records to source URLs first. After migration, URL-based tracking is used.

The progress ledger is stored inside the existing SQLite database, so it is
backed up together with the scraped records. Do not delete the
`scrape_progress` table if you want URL-based resume to work.

### Names and years

Fragrantica's URLs have already lost apostrophes, periods, ampersands and
accents (`Victoria-s-Secret`), so building names from the URL gives
`Victoria S Secret`. `perfumes_scraper.py` therefore looks through the page's own
text for the words whose letters and digits match the URL and keeps them as
written there (`Victoria's Secret`, `D.S. & Durga`, `Bohème`). If nothing on the
page matches, it falls back to the URL's spelling. The launch year comes from the
page title, or from the "launched in YYYY" sentence when the title has none.

Each row records where its names came from (`title_source` / `brand_source`,
either `page` or `url`) and the raw page title inside the `perfume` JSON, so
names and years can be re-derived later without re-crawling. `clean.py` leaves
`page` names exactly as scraped and only re-cases the `url` ones.

### Trying it before a long crawl

Check the extraction on a few pages with a scratch database first (`--limit` counts pages still to do):

```bash
python perfumes_scraper.py --db scratch.sqlite --limit 20
```

Each printed row shows `names_from`, which should be `page/page` for nearly
every perfume. The final lines count how many names came from the page and how
many rows have a year, accords, notes, longevity and sillage. A field that is
near zero means the site's page layout changed and its selectors need updating,
which is much cheaper to find out here than after a full crawl. Delete
`scratch.sqlite` afterwards.

Fragrantica redesigned its pages after the original data was collected, so
`perfumes_scraper.py` reads both layouts (the new one first). Accords and notes are in
the page HTML. Longevity and sillage are drawn by the page's script, so the browser client
scrolls down until they appear (or a "Reviews" heading comes into view, or about ten seconds pass; the page as served only has empty boxes, so it waits for the drawn rows) before reading the page. The site
shows large counts abbreviated (`2.6k`), so those votes are stored as rounded integers
(2600). Its levels (very weak..eternal, intimate..enormous) are stored by position under
the original names (`poor`..`very long lasting`, `soft`..`enormous`). A perfume with no
votes is stored with every count 0. `python tests/test_votes.py` checks the parsing against a saved
copy of the section, and `python tests/run_all.py` runs every offline test script. `python tests/test_manifest.py` checks the manifest crawler's link
matching, scrolling, checkpoints and empty-page handling without a browser.

### Vote charts

Besides longevity and sillage, each perfume page's **gender** chart (female, more female,
unisex, more male, male) and **when to wear** chart (winter, spring, summer, fall, day,
night) are stored as vote counts in the `gender` and `when_to_wear` columns. The perfumers named on the page are stored in `perfumers` (name and `/noses/` slug; `perfumers` in `app.db`). Top, middle and
base notes are stored in `notes`; a page that shows one flat list of notes (no pyramid) is
stored under `general` and kept in its own `general` group in `data/exports/perfumes_clean.json` and `app.db`.
A chart nobody has voted on is stored with every count 0 (and every share 0); a chart missing from the page is `null`.
A perfume whose page gives no launch year has no year (`null`; shown as unknown). `clean.py` normalizes every chart into
`{"votes": {...}, "share": {...}}` in `data/exports/perfumes_clean.json`: for longevity, sillage and gender
the shares add up to 1; for when to wear a voter can pick several seasons, so each level is
shown relative to the strongest one (the strongest is 1.0). `build_app_db.py` writes them
to the `perfume_votes` table (`perfume_id, chart, option, position, votes, share`), and
`popularity` is the number of longevity votes. `python tests/test_pipeline.py` runs a saved
page through scrape, export, clean and the app database.

### Blocking ads (optional)

`--block-ads` (on `perfumes_scraper.py`, `notes_scraper.py` and `dry_test.py`) aborts every
request to a host other than `fragrantica.com`, `fimgs.net` and Cloudflare, so the ~45 ad,
tracker and cookie-sync hosts on a page never load. The allowlist is `ALLOWED_HOSTS` in
`browser_client.py`. When the browser closes it prints how many requests were allowed and
blocked and the busiest blocked hosts; if vote charts come back empty with it on, check that
list for a host the page needs and add it. It also drops font and video requests to the allowed hosts, which the scraper never reads
(page images stay, because without them the page is shorter and the lazy vote cards are reached
differently). It is off by default. To see whether it helps,
run the dry test with and without it. It does not remove the site's own sign-up popup
(`#registration-nudge-overlay`); the scroll unlock handles that.

### Rebuilding the database

Progress is tracked per URL in the `scrape_progress` table of `perfumes.sqlite`, so
with the existing (complete) file the scraper has nothing left to do. To re-scrape
everything, move or delete `perfumes.sqlite` first (an old database without that table
is migrated by row position, which would carry over its known gaps), then rerun the
scraper and the export/clean/build steps below. Perfume ids and slugs are derived from
the names, so they change wherever a name changes.

## Trying everything on a small sample first

Before a full crawl, run the live dry test:

```bash
python dry_test.py            # about 15 minutes, a few dozen page loads
python dry_test.py --screenshots 5 --designers 2 --notes 5   # smaller, with chart screenshots
```

It runs every stage against the real site in a new `data/dry_runs/dry_run_<time>/` folder, with its own
databases, manifests and images: the URL crawl for one country (3 designers), the notes
index, 15 hand-picked perfume pages from `dry_urls.txt` (apostrophes, accents, very old
ids, no pyramid, few votes, ...) plus 3 from the crawl, 8 note pages, then re-runs, export,
clean and the `app.db` build. Your real data is never written to (the report proves it with a
before/after fingerprint) and the full scrapers are never started.

Each stored value is compared with what the live page displays, read by a separate piece of
JavaScript (`dry_oracle.py`) instead of the scraper's own parsing: title, brand and year,
main accords, top/middle/base notes, and every vote chart (displayed categories and counts
against the stored counts, then the normalized shares against both the counts and the bars the
page draws). Images are checked for a matching URL and for a file that is non-empty and
decodes. Databases are checked for one record per URL and no duplicates after re-running or
with duplicated manifest lines. The result is a PASS / FAIL / UNAVAILABLE line per field and
page in `report.md` (UNAVAILABLE means the page shows nothing there and nothing was stored).

The run stops at the first HTTP 429/403, challenge page or list that never finishes loading
and reports it as BLOCKED; it does not try to get around it. Pages that fail a check are saved
under `failures/` so selectors can be fixed from the real HTML. Exit status: 0 all passed,
1 a check failed, 2 blocked. `python tests/test_dry.py` tests the comparison logic offline.
Once the curated pages pass, `python dry_test.py --random 10` tests 10 pages picked at random from `perfumes_manifest.txt` (it prints the seed; `--seed N` repeats the same sample). A random page that fails is a new case: add it to `dry_urls.txt`.
After a failed run, `python dry_test.py --only-failed dry_run_<time>` re-tests just the perfume pages that failed there (12 pages instead of the whole sample, and no manifest or notes crawl); `--urls FILE` does the same for a list of your own.
`python tests/test_failure_handling.py` checks that things fail safely: image downloads are atomic and validated (a 404 image is skipped, not fatal), blank pages are refused and pages missing vote cards are skipped and kept for diagnosis, a Cloudflare page that clears itself is waited out, and a failed `app.db` build keeps the previous file.

## Pipeline

1. `perfumes_extract_manifest.py` / `notes_extract_manifest.py` - collect page URLs.
2. `perfumes_scraper.py` / `notes_scraper.py` - scrape URLs into
   `perfumes.sqlite` / `notes.sqlite`.
3. `export.py` - export `perfumes.sqlite` to `data/exports/perfumes.json`.
4. `clean.py` - normalize `data/exports/perfumes.json` into `data/exports/perfumes_clean.json`. Perfumes
   whose Fragrantica page has no title are dropped (`--keep-unnamed` keeps them).
5. `build_app_db.py [--in perfumes_clean.json] [--out app.db]` - build the searchable `app.db`; it checks the new file (integrity, row counts) before replacing the old one.

`data/exports/perfumes.json` and `data/exports/perfumes_clean.json` are generated intermediates and
are not tracked in git; `perfumes.sqlite`, `notes.sqlite` and the manifests
are the source data, and `app.db` is the shipped output. After re-scraping or
changing `clean.py`, rerun steps 3-5 so `app.db` stays in sync:

```bash
python export.py && python clean.py --report && python build_app_db.py
```

`verify_scrape.py` checks a finished crawl: every manifest URL scraped exactly once, how
many rows have each field, names taken from the page, and (with `--old old.sqlite`) what
changed versus a previous crawl. Run it before the export/clean/build steps.

`check_db.py` prints sample rows from `perfumes.sqlite` for a quick sanity
check. `db_helper.py` holds the SQLite helpers and URL completion ledger used
by both scrapers.

### Checking the crawl is complete

`perfumes_extract_manifest.py` guards against a page that did not finish loading:

- A **country page with no designers** is fetched once more; if it is still empty the crawl stops
  without moving the checkpoint, so the country is not skipped.
- A **designer page with no perfumes**, or with clearly fewer than the previous
  `perfumes_manifest.txt` had (under 90% and at least 3 fewer), is fetched once more and the longer
  list is kept. Still empty: listed in `data/manifests/perfumes_manifest.empty.txt`. Still short: listed with both
  counts in `data/manifests/perfumes_manifest.short.txt`. The crawl goes on either way.
- At the end (or when the crawl stops) it prints a per-country summary: designers, perfumes found,
  new, empty and short.

### Pages that cannot be scraped

- A page the site no longer has (HTTP 404 or 410) is listed in `perfumes_gone.txt` and never asked for again.
- A server error (HTTP 5xx) is retried twice after a pause before the crawl stops.
- A page that is incomplete (a vote card missing) or unreadable (blank page, malformed URL) is
  skipped and listed once in `perfumes_incomplete.txt` or `perfumes_failed.txt`. It is tried again on
  the next run, up to 3 tries in all; after that it is left alone until you run
  `python perfumes_scraper.py --retry-skipped`. 5 skipped pages in a row stop the crawl, because
  that points to a changed layout, not a bad page.
- A designer page too long to scroll to the end keeps what it found and is listed in
  `data/manifests/perfumes_manifest.short.txt`; the crawl goes on.
- Repeated blocks end the run: 3 in a row, or 5 within the last 20 fetches. A `Retry-After` longer
  than 15 minutes also ends it.

### What each row records

Every row stores `url` (always the canonical `https://www.fragrantica.com/...` form, whatever the
manifest says) and `page_id` (the number at the end of the URL). `url` is unique, so scraping a
page again replaces its row. `clean.py` uses the page id as the perfume's `id`, and `app.db` keeps
the URL as `perfumes.source_url`. Rows without a page id (an older crawl without images) get a hash
of brand, name and year instead. Rows of an older crawl get their URL from the id in the bottle
image's file name. The chart levels live in one place, `charts.py`.

### Being gentle with the site

- **Slowdown:** a rate limit (HTTP 429) or a challenge doubles every pause before a request, up to
  8 times the normal length; 50 requests in a row without one halve it again.
- **Stopping:** three blocks in a row end the run ("the site blocked 3 requests in a row"). Progress
  is saved; wait an hour or more and rerun.
- **Browser profile:** the crawlers keep the browser's cookies in `browser_profile/` between runs, so
  a challenge cleared once stays cleared. `--profile DIR` picks another folder and `--no-profile`
  starts fresh. The dry test always starts fresh.
