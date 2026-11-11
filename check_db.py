import os
import sqlite3

from archiver import paths


def print_sample_rows(db_path=None, limit=10):
    db_path = db_path or paths.database_file('perfumes.sqlite')
    # sqlite3.connect() silently creates an empty file when the path is wrong.
    if not os.path.exists(db_path):
        raise SystemExit(f"Missing {db_path}. Run perfumes_scraper.py first.")

    conn = sqlite3.connect(db_path)
    for row in conn.execute("SELECT * FROM perfumes LIMIT ?", (limit,)):
        print(row)
    conn.close()


if __name__ == "__main__":
    print_sample_rows()
