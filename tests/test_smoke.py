"""Tests for the comparison logic behind smoke_test.py, against a real rendered page (no browser).
Run with: python tests/test_smoke.py"""
import copy
import json
import os
import struct
import sys
import tempfile
import types
import zlib
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

import clean  # noqa: E402
import perfumes_scraper as ps  # noqa: E402
import smoke_oracle as so  # noqa: E402

shown = json.loads((ROOT / "tests/fixtures/oracle_black_orchid.json").read_text())
tree = html.fromstring((ROOT / "tests/fixtures/performance_cards_black_orchid.html").read_text(encoding="utf-8"))
charts = ps.page_charts(tree)
record = {
    "brand": "Tom Ford", "launch_year": "2006",
    "perfume": {"title": "Black Orchid", "image": "o.1018.jpg", "title_source": "page", "brand_source": "page"},
    "main_accords": shown["accords"],
    "notes": {k: shown["notes"][k] for k in ("top", "middle", "base")},
    **charts,
}
identity = {k: k for k in clean.GENDER_KEYS + clean.WHEN_KEYS}
maps = {"longevity": clean.LON_KEYS, "sillage": clean.SIL_KEYS, "gender": identity, "when_to_wear": identity}
cleaned = {
    name: clean._chart(json.dumps(charts[name]), maps[name], clean.CHART_ORDER[name], name in clean.RELATIVE_TO_MAX)
    for name in so.SITE_LABELS
}


def statuses(rec=record, sh=shown, ch=cleaned, problem=None):
    return {c["field"]: c["status"] for c in so.compare_perfume(rec, sh, ch, problem, "1018")}


base = statuses()
assert all(v == so.PASS for v in base.values()), {k: v for k, v in base.items() if v != so.PASS}
print("a correct extraction of the real page passes all {} checks".format(len(base)))


def broken(mutate, expect):
    rec, sh, ch = copy.deepcopy(record), copy.deepcopy(shown), copy.deepcopy(cleaned)
    mutate(rec, sh, ch)
    result = statuses(rec, sh, ch)
    assert result[expect] == so.FAIL, (expect, result[expect])


# each kind of mistake is caught by the check that should catch it
broken(lambda r, s, c: r["longevity"].update({"moderate": "2601"}), "chart: longevity")
broken(lambda r, s, c: r.update({"sillage": None}), "chart: sillage")
broken(lambda r, s, c: r["gender"].update({"male": "1"}), "chart: gender")
broken(lambda r, s, c: r["when_to_wear"].update({"night": "1"}), "chart: when_to_wear")
broken(lambda r, s, c: r["notes"].update({"top": r["notes"]["base"]}), "notes: top")
broken(lambda r, s, c: r["notes"]["middle"].pop(), "notes: middle")
broken(lambda r, s, c: r.update({"main_accords": r["main_accords"][::-1]}), "accords")
broken(lambda r, s, c: r.update({"launch_year": "2007"}), "year")
broken(lambda r, s, c: r.update({"brand": "Tom Ford Beauty"}), "brand")
broken(lambda r, s, c: r["perfume"].update({"title": "Black Orchids"}), "title")
broken(lambda r, s, c: r["perfume"].update({"title_source": "url"}), "title from page (not URL)")
broken(lambda r, s, c: r["perfume"].update({"image": "o.9999.jpg"}), "image name")
broken(lambda r, s, c: c["longevity"]["share"].update({"moderate": 0.5}), "normalized: longevity")
broken(lambda r, s, c: c["when_to_wear"]["share"].update({"night": 0.5}), "normalized: when_to_wear")
broken(lambda r, s, c: s["charts"]["longevity"][0].update({"label": "none"}), "chart: longevity")
assert statuses(problem="file is missing")["image file"] == so.FAIL
print("15 kinds of wrong extraction are each detected")

# a page with no votes must be stored as missing, and is reported as unavailable
nothing = copy.deepcopy(shown)
nothing["charts"] = {}
rec = copy.deepcopy(record)
rec.update(longevity=None, sillage=None, gender=None, when_to_wear=None)
result = statuses(rec, nothing, {})
assert result["chart: gender"] == so.UNAVAILABLE and result["normalized: gender"] == so.UNAVAILABLE
print("pages without votes are reported as unavailable, not passed silently")

# images
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    raw = b"".join(b"\x00" + os.urandom(96) for _ in range(64))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 64, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    (tmp / "ok.png").write_bytes(png)
    (tmp / "empty.jpg").write_bytes(b"")
    (tmp / "html.jpg").write_bytes(b"<html>blocked</html>" * 100)
    (tmp / "cut.png").write_bytes(png[: len(png) // 2])
    assert so.image_problem(tmp / "ok.png") is None, so.image_problem(tmp / "ok.png")
    assert "bytes" in so.image_problem(tmp / "empty.jpg")
    assert "not a JPEG" in so.image_problem(tmp / "html.jpg")
    assert so.image_problem(tmp / "cut.png") is not None
    assert so.image_problem(tmp / "absent.jpg") == "file is missing"
print("image validation catches missing, empty, wrong-type and truncated files OK")

# note pages: a real page's displayed photos against what was stored
shown_note = {"title": "Bergamot", "images": [
    "https://fimgs.net/mdimg/sastojci/t.75.jpg?1733161295",
    "https://www.fragrantica.com/mdimg/sastojci/t2.75.jpg?1733161288",
    "https://www.fragrantica.com/mdimg/sastojci/t3.75.jpg?1733161293"]}
names = ["t.75.jpg", "t2.75.jpg", "t3.75.jpg"]
good = so.compare_note("Bergamot", names, shown_note, dict.fromkeys(names))
assert all(c["status"] == so.PASS for c in good), good
assert so.compare_note("Bergamot", names[:2], shown_note, dict.fromkeys(names[:2]))[1]["status"] == so.FAIL
assert so.compare_note("Bergamot ", names, {**shown_note, "title": "Bergamot Oil"}, dict.fromkeys(names))[0]["status"] == so.FAIL
assert so.compare_note("X", [], {"title": "X", "images": []}, {})[1]["status"] == so.UNAVAILABLE
assert so.compare_note("Bergamot", names, shown_note, {**dict.fromkeys(names), "t2.75.jpg": "file is missing"})[2]["status"] == so.FAIL
print("note page comparison OK")
