"""End to end on saved pages, no browser: scrape -> sqlite -> export -> clean -> app.db.
Run with: python tests/test_pipeline.py"""
import json
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

from lxml import html

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from archiver import paths  # noqa: E402
paths.use_flat()   # these tests work in a scratch folder and expect plain file names
try:
    import patchright  # noqa: F401
except ImportError:
    stub = types.ModuleType("patchright.sync_api")
    stub.Error, stub.sync_playwright = Exception, None
    sys.modules["patchright"], sys.modules["patchright.sync_api"] = types.ModuleType("patchright"), stub

import build_app_db  # noqa: E402
import clean  # noqa: E402
from archiver import db_helper  # noqa: E402
import export  # noqa: E402
import perfumes_scraper as ps  # noqa: E402

tree = html.fromstring((ROOT / "tests/fixtures/performance_cards_black_orchid.html").read_text(encoding="utf-8"))
charts = ps.page_charts(tree)
assert charts["gender"] == {"female": "1500", "more female": "2000", "unisex": "6300", "more male": "823", "male": "596"}
assert charts["when_to_wear"] == {"winter": "12800", "spring": "3200", "summer": "1800", "fall": "10600", "day": "3700", "night": "13000"}
assert charts["longevity"]["very long lasting"] == "5300" and charts["sillage"]["enormous"] == "4000"
# a wrong label set or shape is rejected instead of being stored under the wrong names
bad = html.fromstring(html.tostring(tree).decode().replace(">unisex<", ">neutral<").encode())
assert ps.page_charts(bad)["gender"] is None
print("charts extracted OK")

# ---- the overall rating: "Perfume rating 3.88 out of 5 with 33,049 votes" ----
rated = html.fromstring((ROOT / "tests/fixtures/rating_black_orchid.html").read_text(encoding="utf-8"))
assert ps.page_rating(rated) == ("3.88", "33049")        # the count comes from the content attribute, not "33,049"
unrated = html.fromstring((ROOT / "tests/fixtures/rating_none_bellini.html").read_text(encoding="utf-8"))
assert ps.page_rating(unrated) == (None, None)
assert ps.page_rating(html.fromstring("<html><body><p itemprop='aggregateRating'><span itemprop='ratingValue'>4.0</span>"
                                      "<span itemprop='ratingCount'>1,065</span></p></body></html>")) == ("4", "1065")
assert ps.page_rating(html.fromstring("<html><body><p itemprop='aggregateRating'><span itemprop='ratingValue'>n/a</span>"
                                      "</p></body></html>")) == (None, None)
print("rating extracted OK")

import clean as _clean  # noqa: E402
assert _clean._to_rating("3.88") == 3.88 and _clean._to_rating(None) is None and _clean._to_rating("7") is None
assert _clean._to_rating("abc") is None and _clean._to_rating("0") == 0.0
assert _clean._parse_perfumers('[{"name": "David  Apel", "slug": "David_Apel"}, {"slug": "x"}]') == [{"name": "David Apel", "slug": "David_Apel"}]
assert _clean._parse_perfumers("null") == [] and _clean._parse_perfumers(None) == []
assert _clean._parse_notes('{"general": ["Milk", "Caramel"]}') == {"top": [], "middle": [], "base": [], "general": ["Milk", "Caramel"]}
assert _clean._parse_notes({"top": ["A"], "base": ["B"]})["general"] == []
print("flat note lists keep their own general group OK")

record = ("Tom Ford", json.dumps({"title": "Black Orchid", "image": "o.1018.jpg", "title_source": "page", "brand_source": "page"}),
          "2006", json.dumps(["warm spicy"]), json.dumps({"top": ["Truffle"], "middle": ["Orchid"], "base": ["Patchouli"]}),
          json.dumps(charts["longevity"]), json.dumps(charts["sillage"]), json.dumps(charts["gender"]),
          json.dumps(charts["when_to_wear"]), json.dumps([{"name": "David Apel", "slug": "David_Apel"}]), "female",
          *ps.page_rating(rated))
empty = ("A", json.dumps({"title": "Nothing", "title_source": "page", "brand_source": "page"}), None, "null", "null", "null", "null", "null", "null", "[]", None, *ps.page_rating(unrated))

with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    db = db_helper.PerfumesDBHelper("perfumes.sqlite")
    db.setup()
    db.add_record_for_url("u1", record)
    db.add_record_for_url("u2", empty)
    db.close()
    export.export_to_json()
    clean.clean(report=False)
    with open("perfumes_clean.json", encoding="utf-8") as clean_file:
        data = json.load(clean_file)
    p = next(x for x in data if x["name"] == "Black Orchid")
    lon = p["charts"]["longevity"]
    assert abs(sum(lon["share"].values()) - 1) < 1e-3 and lon["votes"]["moderate"] == 2600
    assert abs(sum(p["charts"]["gender"]["share"].values()) - 1) < 1e-3
    assert p["charts"]["when_to_wear"]["share"]["night"] == 1.0           # relative to the strongest
    assert p["charts"]["when_to_wear"]["share"]["winter"] == round(12800 / 13000, 4)
    assert p["popularity"] == sum(lon["votes"].values()) == 14519
    assert next(x for x in data if x["name"] == "Nothing")["charts"] == {}
    # the official label is kept next to the votes (which say unisex): stored, cleaned, and in app.db
    assert p["audience"] == "female" and next(x for x in data if x["name"] == "Nothing")["audience"] is None
    # the rating and how many people gave it: numbers in clean and app.db, empty for an unrated perfume
    nothing = next(x for x in data if x["name"] == "Nothing")
    assert p["rating"] == 3.88 and p["rating_votes"] == 33049 and nothing["rating"] is None and nothing["rating_votes"] is None
    build_app_db.main([])
    conn = sqlite3.connect("app.db")
    assert conn.execute("SELECT name, rating, rating_votes FROM perfumes ORDER BY name").fetchall() == [
        ("Black Orchid", 3.88, 33049), ("Nothing", None, None)]
    assert conn.execute("SELECT typeof(rating), typeof(rating_votes) FROM perfumes WHERE name='Black Orchid'").fetchone() == ("real", "integer")
    rows = conn.execute("SELECT chart, option, position, votes, share FROM perfume_votes ORDER BY chart, position").fetchall()
    assert len(rows) == 5 + 4 + 5 + 6 and all(0 <= r[4] <= 1 for r in rows)
    assert conn.execute("SELECT COUNT(*) FROM notes WHERE kind IN ('top','middle','base')").fetchone()[0] == 3
    assert dict(conn.execute("SELECT name, audience FROM perfumes")) == {"Black Orchid": "female", "Nothing": None}
    print("scrape -> export -> clean -> app.db OK ({} vote rows)".format(len(rows)))


import sqlite3  # noqa: E402
import tempfile  # noqa: E402

# ---- a database made before the rating columns existed gets them added, keeping its rows ----
with tempfile.TemporaryDirectory() as tmp:
    old = sqlite3.connect(os.path.join(tmp, "old.sqlite"))
    old.execute("CREATE TABLE perfumes (brand TEXT, perfume TEXT, launch_year TEXT, main_accords TEXT, notes TEXT, longevity TEXT, "
                "sillage TEXT, gender TEXT, when_to_wear TEXT, perfumers TEXT, audience TEXT, url TEXT, page_id TEXT)")
    old.execute("INSERT INTO perfumes (brand, url) VALUES ('Tom Ford', 'https://www.fragrantica.com/perfume/Tom-Ford/Black-Orchid-1018.html')")
    old.commit(); old.close()
    migrated = db_helper.PerfumesDBHelper(os.path.join(tmp, "old.sqlite"))
    migrated.setup(); migrated.setup()                        # setting up twice is harmless
    assert migrated.conn.execute("SELECT brand, rating, rating_votes FROM perfumes").fetchall() == [("Tom Ford", None, None)]
    migrated.close()
print("older databases gain the rating columns OK")

from archiver import db_helper  # noqa: E402

# ---- every row knows its page: url + page id, one row per page, ids in clean and app.db ----
U1 = "https://www.fragrantica.com/perfume/Tom-Ford/Black-Orchid-1018.html"
U2 = "https://www.fragrantica.com/perfume/Creed/Aventus-9828.html"
with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    db = db_helper.PerfumesDBHelper("perfumes.sqlite")
    db.setup()
    db.add_record_for_url(U1.replace("https://", "http://"), record)      # an http URL is stored as https
    db.add_record_for_url(U2, empty)
    db.add_record_for_url(U1, record[:2] + ("2007",) + record[3:])        # the same page again: replaced
    assert db.number_of_records() == 2, "scraping a page again must not add a second row"
    assert db.conn.execute("SELECT url, page_id, launch_year FROM perfumes WHERE page_id='1018'").fetchall() == [(U1, "1018", "2007")]
    assert db.completed_urls() == {U1, U2}

    db.close()

    export.export_to_json()
    clean.clean(report=False)
    with open("perfumes_clean.json", encoding="utf-8") as clean_file:
        rows = {x["name"]: x for x in json.load(clean_file)}
    assert rows["Black Orchid"]["id"] == "1018" and rows["Black Orchid"]["url"] == U1
    build_app_db.main([])
    conn = sqlite3.connect("app.db")
    assert conn.execute("SELECT id, source_url FROM perfumes WHERE name='Black Orchid'").fetchone() == ("1018", U1)
    conn.close()
print("rows carry their url and page id; a page scraped twice has one row; clean and app.db use the id OK")
