"""SQLite helpers for scraper data and URL-based progress tracking."""

import re
import sqlite3
from pathlib import Path

from archiver.urls import canonical_url


class _TableDB:
    """A SQLite file holding one scraper table and its completed URL ledger."""

    table = None
    columns = ()
    # Every row also records the page it came from; `url` is unique, so scraping a page again
    # updates its row instead of adding a second one.
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
        self.conn.commit()

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

class NotesDBHelper(_TableDB):
    table = "notes"
    columns = ("note", "images")
