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
try:
    import patchright  # noqa: F401
except ImportError:
    stub = types.ModuleType("patchright.sync_api")
    stub.Error, stub.sync_playwright = Exception, None
    sys.modules["patchright"], sys.modules["patchright.sync_api"] = types.ModuleType("patchright"), stub

import build_app_db  # noqa: E402
import clean  # noqa: E402
import db_helper  # noqa: E402
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

record = ("Tom Ford", json.dumps({"title": "Black Orchid", "image": "o.1018.jpg", "title_source": "page", "brand_source": "page"}),
          "2006", json.dumps(["warm spicy"]), json.dumps({"top": ["Truffle"], "middle": ["Orchid"], "base": ["Patchouli"]}),
          json.dumps(charts["longevity"]), json.dumps(charts["sillage"]), json.dumps(charts["gender"]),
          json.dumps(charts["when_to_wear"]))
empty = ("A", json.dumps({"title": "Nothing", "title_source": "page", "brand_source": "page"}), None, "null", "null", "null", "null", "null", "null")

with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    # an older database (7 columns) gains the new columns instead of failing
    old = sqlite3.connect("perfumes.sqlite")
    old.execute("CREATE TABLE perfumes (brand TEXT, perfume TEXT, launch_year TEXT, main_accords TEXT, notes TEXT, longevity TEXT, sillage TEXT)")
    old.commit()
    old.close()
    db = db_helper.PerfumesDBHelper("perfumes.sqlite")
    db.setup()
    db.add_record_for_url("u1", record)
    db.add_record_for_url("u2", empty)
    db.close()
    export.export_to_json()
    clean.clean(report=False)
    data = json.load(open("perfumes_clean.json", encoding="utf-8"))
    p = next(x for x in data if x["name"] == "Black Orchid")
    lon = p["charts"]["longevity"]
    assert abs(sum(lon["share"].values()) - 1) < 1e-3 and lon["votes"]["moderate"] == 2600
    assert abs(sum(p["charts"]["gender"]["share"].values()) - 1) < 1e-3
    assert p["charts"]["when_to_wear"]["share"]["night"] == 1.0           # relative to the strongest
    assert p["charts"]["when_to_wear"]["share"]["winter"] == round(12800 / 13000, 4)
    assert p["popularity"] == sum(lon["votes"].values()) == 14519
    assert next(x for x in data if x["name"] == "Nothing")["charts"] == {}
    build_app_db.main()
    conn = sqlite3.connect("app.db")
    rows = conn.execute("SELECT chart, option, position, votes, share FROM perfume_votes ORDER BY chart, position").fetchall()
    assert len(rows) == 5 + 4 + 5 + 6 and all(0 <= r[4] <= 1 for r in rows)
    assert conn.execute("SELECT COUNT(*) FROM notes WHERE kind IN ('top','middle','base')").fetchone()[0] == 3
    print("scrape -> export -> clean -> app.db OK ({} vote rows)".format(len(rows)))
