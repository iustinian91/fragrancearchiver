"""SQLite helpers for scraper data and URL-based progress tracking."""

import json
import re
import sqlite3

from urls import canonical_url


class _TableDB:
    """A SQLite file holding one scraper table and its completed URL ledger."""

    table = None
    columns = ()

    def __init__(self, db_name):
        self.db_name = db_name
        self.conn = sqlite3.connect(db_name)

    def setup(self):
        definitions = ", ".join("{} TEXT".format(column) for column in self.columns)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS {} ({})".format(self.table, definitions)
        )
        # A database made by an older version lacks the newer columns; add them in place.
        have = {row[1] for row in self.conn.execute("PRAGMA table_info({})".format(self.table))}
        for column in self.columns:
            if column not in have:
                self.conn.execute("ALTER TABLE {} ADD COLUMN {} TEXT".format(self.table, column))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scrape_progress (
                url TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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

    def add_record(self, args):
        """Insert a data row (legacy helper; new scrapers should use add_record_for_url)."""
        self.conn.execute(
            "INSERT INTO {} ({}) VALUES ({})".format(
                self.table, ", ".join(self.columns), ", ".join("?" * len(self.columns))
            ),
            args,
        )
        self.conn.commit()

    def add_record_for_url(self, url, args):
        """Atomically save a scraped record and mark its source URL complete."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO {} ({}) VALUES ({})".format(
                    self.table,
                    ", ".join(self.columns),
                    ", ".join("?" * len(self.columns)),
                ),
                args,
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO scrape_progress (url) VALUES (?)",
                (canonical_url(url),),
            )

    def completed_urls(self):
        """Return every source URL already committed to this database."""
        rows = self.conn.execute("SELECT url FROM scrape_progress")
        return {row[0] for row in rows}

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
