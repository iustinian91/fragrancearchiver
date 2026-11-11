"""SQLite helpers for scraper data and URL-based progress tracking."""

import json
import re
import sqlite3
from pathlib import Path

from archiver.urls import canonical_url


class _TableDB:
    """A SQLite file holding one scraper table and its completed URL ledger."""

    table = None
    columns = ()
    # Every row also records the page it came from; `url` is unique, so scraping a page again
    # updates its row instead of adding a second one. (SQL NULL for rows of an older crawl.)
    source_columns = ("url", "page_id")

    def __init__(self, db_name):
        self.db_name = db_name
        Path(db_name).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_name)

    def setup(self):
        every_column = self.columns + self.source_columns
        definitions = ", ".join("{} TEXT".format(column) for column in every_column)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS {} ({})".format(self.table, definitions)
        )
        # A database made by an older version lacks the newer columns; add them in place.
        have = {row[1] for row in self.conn.execute("PRAGMA table_info({})".format(self.table))}
        for column in every_column:
            if column not in have:
                self.conn.execute("ALTER TABLE {} ADD COLUMN {} TEXT".format(self.table, column))
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_{0}_url ON {0}(url)".format(self.table))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scrape_progress (
                url TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gone_urls (
                url TEXT PRIMARY KEY,
                reason TEXT,
                seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skip_attempts (
                url TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL,
                reason TEXT
            )
            """
        )
        self._canonicalize_progress()
        self.conn.commit()

    def _canonicalize_progress(self):
        """Rewrite ledger URLs written as http:// (older crawls) to the canonical https:// form,
        keeping when each was completed, so the same page is not scraped a second time."""
        stale = [url for (url,) in self.conn.execute("SELECT url FROM scrape_progress")
                 if canonical_url(url) != url]
        for url in stale:
            self.conn.execute(
                "INSERT OR IGNORE INTO scrape_progress (url, completed_at) "
                "SELECT ?, completed_at FROM scrape_progress WHERE url = ?", (canonical_url(url), url))
            self.conn.execute("DELETE FROM scrape_progress WHERE url = ?", (url,))

    def add_record_for_url(self, url, args):
        """Atomically save a scraped record with its page URL and id, and mark the URL complete.
        A page scraped again replaces its earlier row."""
        url = canonical_url(url)
        every_column = self.columns + self.source_columns
        with self.conn:
            self.conn.execute(
                "INSERT INTO {table} ({columns}) VALUES ({marks}) ON CONFLICT(url) DO UPDATE SET {updates}".format(
                    table=self.table,
                    columns=", ".join(every_column),
                    marks=", ".join("?" * len(every_column)),
                    updates=", ".join("{0}=excluded.{0}".format(c) for c in every_column),
                ),
                tuple(args) + (url, _page_id(url)),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO scrape_progress (url) VALUES (?)",
                (url,),
            )
            self.conn.execute("DELETE FROM skip_attempts WHERE url = ?", (url,))

    def completed_urls(self):
        """Return every source URL already committed to this database."""
        rows = self.conn.execute("SELECT url FROM scrape_progress")
        return {row[0] for row in rows}

    def mark_gone(self, url, reason):
        """Remember that the site no longer has this page, so later runs do not ask for it."""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO gone_urls (url, reason) VALUES (?, ?)", (canonical_url(url), reason))

    def note_skip(self, url, reason):
        """Count one more failed try at `url`; returns how many tries it has had."""
        url = canonical_url(url)
        with self.conn:
            self.conn.execute(
                "INSERT INTO skip_attempts (url, attempts, reason) VALUES (?, 1, ?) "
                "ON CONFLICT(url) DO UPDATE SET attempts = attempts + 1, reason = excluded.reason",
                (url, reason))
        return self.conn.execute("SELECT attempts FROM skip_attempts WHERE url = ?", (url,)).fetchone()[0]

    def given_up_urls(self, max_attempts):
        """Pages that failed `max_attempts` times or more: not asked for again until reset."""
        return {row[0] for row in self.conn.execute(
            "SELECT url FROM skip_attempts WHERE attempts >= ?", (max_attempts,))}

    def reset_skips(self):
        with self.conn:
            self.conn.execute("DELETE FROM skip_attempts")

    def gone_urls(self):
        return {row[0] for row in self.conn.execute("SELECT url FROM gone_urls")}

    def migrate_legacy_progress(self, manifest_urls):
        """
        Map legacy row-count progress to the original manifest order once.

        Older scraper versions processed URLs sequentially but did not store them.
        This migration preserves that behavior for existing databases.
        """
        tracked = self.conn.execute(
            "SELECT COUNT(*) FROM scrape_progress"
        ).fetchone()[0]
        if tracked:
            return 0

        existing_records = self.number_of_records()
        if not existing_records:
            return 0
        if existing_records > len(manifest_urls):
            raise RuntimeError(
                "{} contains {} records but the current manifest has only {} URLs. "
                "Cannot safely migrate legacy progress; restore the original manifest "
                "or reconcile the database before continuing.".format(
                    self.db_name, existing_records, len(manifest_urls)
                )
            )

        legacy_urls = list(dict.fromkeys(manifest_urls))[:existing_records]
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO scrape_progress (url) VALUES (?)",
                ((url,) for url in legacy_urls),
            )
            # The older crawl went through the manifest in order, so the k-th row is the k-th URL.
            rowids = [r[0] for r in self.conn.execute(
                "SELECT rowid FROM {} WHERE url IS NULL ORDER BY rowid".format(self.table))]
            for rowid, url in zip(rowids, legacy_urls):
                self.conn.execute("UPDATE {} SET url = ?, page_id = ? WHERE rowid = ?".format(self.table),
                                  (url, _page_id(url), rowid))
        print(
            "Migrated {} legacy records to URL-based progress using the first {} "
            "manifest entries. Verify that the manifest order has not changed.".format(
                len(legacy_urls), existing_records
            ),
            flush=True,
        )
        return len(legacy_urls)

    def number_of_records(self):
        return self.conn.execute("SELECT COUNT(*) FROM {}".format(self.table)).fetchone()[0]

    def close(self):
        self.conn.close()


def _page_id(text):
    """The numeric Fragrantica id at the end of a page URL (...-12345.html), or None."""
    match = re.search(r"-(\d+)\.html?$", text or "")
    return match.group(1) if match else None


class PerfumesDBHelper(_TableDB):
    table = "perfumes"
    columns = (
        "brand", "perfume", "launch_year", "main_accords", "notes", "longevity", "sillage",
        "gender", "when_to_wear", "perfumers",
    )

    def migrate_legacy_progress(self, manifest_urls):
        """Mark URLs complete by matching the bottle image id (o.<id>.jpg) saved in each
        row against the id in the URL. Unlike counting rows, this stays correct when the
        manifest changed or the old crawl skipped or repeated a page; those URLs are simply
        scraped again. Falls back to row counting only if no row carries an image id."""
        if self.conn.execute("SELECT COUNT(*) FROM scrape_progress").fetchone()[0]:
            return 0
        ids = set()
        for (raw,) in self.conn.execute("SELECT perfume FROM perfumes"):
            try:
                image = json.loads(raw).get("image")
            except (ValueError, AttributeError, TypeError):
                continue
            match = re.match(r"o\.(\d+)\.", image or "")
            if match:
                ids.add(match.group(1))
        if not ids:
            marked = super().migrate_legacy_progress(manifest_urls)
            self._warn_legacy_rows()
            return marked
        done = [url for url in dict.fromkeys(manifest_urls) if _page_id(url) in ids]
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO scrape_progress (url) VALUES (?)",
                ((url,) for url in done),
            )
        print(
            "Migrated {} legacy records to URL-based progress by matching image ids "
            "({} of {} manifest URLs still to scrape).".format(
                len(done), len(set(manifest_urls)) - len(done), len(set(manifest_urls))
            ),
            flush=True,
        )
        self._warn_legacy_rows()
        return len(done)

    def backfill_urls(self, manifest_urls):
        """Give rows that have no URL yet (an older crawl) the URL of the page whose id their bottle
        image carries (o.<id>.jpg). Rows with no image, or whose id is not in the manifest, stay
        without one."""
        by_id = {}
        for url in manifest_urls:
            by_id.setdefault(_page_id(url), url)
        filled = 0
        for rowid, raw in self.conn.execute(
                "SELECT rowid, perfume FROM perfumes WHERE url IS NULL").fetchall():
            try:
                image = json.loads(raw).get("image")
            except (ValueError, AttributeError, TypeError):
                continue
            match = re.match(r"o\.(\d+)\.", image or "")
            url = by_id.get(match.group(1)) if match else None
            if url:
                try:
                    with self.conn:
                        self.conn.execute("UPDATE perfumes SET url = ?, page_id = ? WHERE rowid = ?",
                                          (url, _page_id(url), rowid))
                    filled += 1
                except sqlite3.IntegrityError:
                    pass   # two rows carry the same image id; the second is left without a URL
        return filled

    def _warn_legacy_rows(self):
        """Rows from an older crawl have no gender, when-to-wear or perfumers (SQL NULL, where a
        current row stores the text 'null'); once marked complete they are never scraped again."""
        old = self.conn.execute("SELECT COUNT(*) FROM perfumes WHERE when_to_wear IS NULL").fetchone()[0]
        if old:
            print(
                "WARNING: {} rows come from an older crawl and have no gender, when-to-wear or "
                "perfumers. They are now marked complete and will not be scraped again. For the "
                "full data, start from an empty database (--db perfumes_new.sqlite).".format(old),
                flush=True)


class NotesDBHelper(_TableDB):
    table = "notes"
    columns = ("note", "images")
