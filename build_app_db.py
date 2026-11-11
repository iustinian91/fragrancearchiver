# build_app_db.py
import json
import os
import sqlite3
from pathlib import Path

DB = "app.db"
DATA = "perfumes_clean.json"   # output of clean.py

def main():
    if not Path(DATA).exists():
        raise SystemExit(f"Missing {DATA}. Run clean.py first.")

    # Build into a temporary file and swap it in at the end, so a failed build leaves the
    # existing app.db untouched. Clear leftovers of an earlier failed build first.
    building = DB + ".building"
    for path in (building, building + "-wal", building + "-shm"):
        if os.path.exists(path):
            os.remove(path)

    conn = sqlite3.connect(building)
    cur = conn.cursor()

    # One-shot build into a fresh file: a failed run is simply re-run from scratch, so skip the
    # journal. Deliberately not WAL: that mode is stored in the file header, and a WAL-mode
    # app.db can't be opened from read-only locations (bundled assets, read-only mounts).
    cur.executescript("""
    PRAGMA journal_mode=OFF;
    PRAGMA synchronous=OFF;
    PRAGMA temp_store=MEMORY;
    """)

    # Schema
    cur.executescript("""
    CREATE TABLE perfumes(
      id TEXT PRIMARY KEY,
      brand TEXT NOT NULL,
      name TEXT NOT NULL,
      image TEXT,                  -- optional image/filename/url
      year INTEGER,
      slug TEXT NOT NULL UNIQUE,
      popularity INTEGER DEFAULT 0
    );

    CREATE TABLE notes(
      perfume_id TEXT NOT NULL,
      kind TEXT CHECK(kind IN ('top','middle','base','general')) NOT NULL,
      note TEXT NOT NULL,
      PRIMARY KEY (perfume_id, kind, note),
      FOREIGN KEY (perfume_id) REFERENCES perfumes(id) ON DELETE CASCADE
    );

    CREATE TABLE perfumers(
      perfume_id TEXT NOT NULL,
      name TEXT NOT NULL,
      slug TEXT NOT NULL,
      rank INTEGER,                -- order on the page
      PRIMARY KEY (perfume_id, slug),
      FOREIGN KEY (perfume_id) REFERENCES perfumes(id) ON DELETE CASCADE
    );

    CREATE TABLE accords(
      perfume_id TEXT NOT NULL,
      accord TEXT NOT NULL,
      rank INTEGER,                -- preserves order from JSON
      PRIMARY KEY (perfume_id, accord),
      FOREIGN KEY (perfume_id) REFERENCES perfumes(id) ON DELETE CASCADE
    );

    -- Vote charts (longevity, sillage, gender, when_to_wear). `share` is votes / total votes for
    -- the single-choice charts and votes / the strongest level for when_to_wear (a voter can
    -- pick several seasons), so it is always between 0 and 1. `position` keeps display order.
    CREATE TABLE perfume_votes(
      perfume_id TEXT NOT NULL,
      chart TEXT NOT NULL,
      option TEXT NOT NULL,
      position INTEGER NOT NULL,
      votes INTEGER NOT NULL,
      share REAL NOT NULL,
      PRIMARY KEY (perfume_id, chart, option),
      FOREIGN KEY (perfume_id) REFERENCES perfumes(id) ON DELETE CASCADE
    );

    -- Full-text search across brand, name, and combined notes/accords
    CREATE VIRTUAL TABLE perfume_fts USING fts5(
      brand, name, notes,
      content='',
      tokenize='porter'
    );

    CREATE INDEX idx_perfumes_brand ON perfumes(brand);
    CREATE INDEX idx_perfumes_year  ON perfumes(year);
    CREATE INDEX idx_notes_note     ON notes(note);
    CREATE INDEX idx_accords_accord ON accords(accord);
    CREATE INDEX idx_perfumers_name ON perfumers(name);
    CREATE INDEX idx_votes_chart    ON perfume_votes(chart, option);
    """)

    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)

    perf_rows, note_rows, acc_rows, fts_rows, vote_rows, perfumer_rows = [], [], [], [], [], []
    for rowid, p in enumerate(data, start=1):
        image = p.get("image")
        # Explicit rowid: perfume_fts is contentless, so search hits join back via perfume_fts.rowid = perfumes.rowid
        perf_rows.append((
            rowid, p["id"], p["brand"], p["name"], image, p.get("year"), p["slug"], p.get("popularity", 0)
        ))

        all_notes = []
        for kind in ("top", "middle", "base", "general"):
            for n in p.get("notes", {}).get(kind, []):
                note_rows.append((p["id"], kind, n))
                all_notes.append(n)

        for chart, data_ in p.get("charts", {}).items():
            for position, (option, votes) in enumerate(data_["votes"].items(), start=1):
                vote_rows.append((p["id"], chart, option, position, votes, data_["share"][option]))

        for idx, person in enumerate(p.get("perfumers", []), start=1):
            perfumer_rows.append((p["id"], person["name"], person["slug"], idx))

        # main_accords (ordered)
        for idx, a in enumerate(p.get("main_accords", []), start=1):
            acc_rows.append((p["id"], a, idx))

        # FTS text: brand, name, notes + accords
        fts_rows.append((
            rowid, p["brand"], p["name"], " ".join(all_notes + p.get("main_accords", []))
        ))

    with conn:
        cur.executemany(
            "INSERT INTO perfumes(rowid,id,brand,name,image,year,slug,popularity) VALUES (?,?,?,?,?,?,?,?)",
            perf_rows
        )
        cur.executemany(
            "INSERT INTO notes(perfume_id,kind,note) VALUES (?,?,?)",
            note_rows
        )
        cur.executemany(
            "INSERT INTO perfume_votes(perfume_id,chart,option,position,votes,share) VALUES (?,?,?,?,?,?)",
            vote_rows
        )
        cur.executemany(
            "INSERT OR IGNORE INTO perfumers(perfume_id,name,slug,rank) VALUES (?,?,?,?)",
            perfumer_rows
        )
        cur.executemany(
            "INSERT INTO accords(perfume_id,accord,rank) VALUES (?,?,?)",
            acc_rows
        )
        cur.executemany(
            "INSERT INTO perfume_fts(rowid,brand,name,notes) VALUES (?,?,?,?)",
            fts_rows
        )

    conn.close()
    # Also clear sidecar files of an older WAL-mode app.db: a stale -wal/-shm could otherwise
    # be applied to the new database.
    for suffix in ("-wal", "-shm"):
        if os.path.exists(DB + suffix):
            os.remove(DB + suffix)
    os.replace(building, DB)
    print(f"✅ Built {DB} with {len(perf_rows)} perfumes")

if __name__ == "__main__":
    main()
