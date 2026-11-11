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
    "perfumers": shown["perfumers"],
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

# a page without a pyramid shows one flat list; it is compared, not skipped
flat = {"general": ["Milk", "Caramel", "Vanilla"]}
good_flat = so.compare_notes(flat, {"notes": {"general": ["Milk", "Caramel", "Vanilla"]}})
assert [c["status"] for c in good_flat] == [so.PASS], good_flat
bad_flat = so.compare_notes({"general": ["Milk"]}, {"notes": {"general": ["Milk", "Caramel", "Vanilla"]}})
assert bad_flat[0]["status"] == so.FAIL
# the stored list must not be ignored when the page shows nothing
assert so.compare_notes({"general": ["Milk"]}, {"notes": {}})[0]["status"] == so.FAIL
# a page with no year is reported as unknown, not as a failure
identity = so.compare_identity(
    {"title": "T", "brand": "B", "title_source": "page", "brand_source": "page", "year": None},
    {"title": "T B for women", "brand": "B", "year": None},
)
year = next(c for c in identity if c["field"] == "year")
assert year["status"] == so.UNAVAILABLE and "unknown" in year["detail"], year
print("flat note lists and unknown years OK")

# --only-failed picks the pages that failed in an earlier run, in the order they were tested
import json as _json  # noqa: E402
import tempfile as _tempfile  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

import smoke_test as st  # noqa: E402

with _tempfile.TemporaryDirectory() as _tmp:
    _run = _Path(_tmp)
    (_run / "perfumes_manifest.txt").write_text(
        "https://www.fragrantica.com/perfume/A/One-1.html\nhttps://www.fragrantica.com/perfume/B/Two-2.html\n"
        "https://www.fragrantica.com/perfume/C/Three-3.html\n")
    (_run / "report.json").write_text(_json.dumps({"checks": [
        {"stage": "perfume pages", "subject": "Three-3.html", "field": "chart: gender", "status": "FAIL", "detail": ""},
        {"stage": "perfume pages", "subject": "One-1.html", "field": "year", "status": "PASS", "detail": ""},
        {"stage": "perfume pages", "subject": "Two-2.html", "field": "chart: sillage", "status": "FAIL", "detail": ""},
        {"stage": "notes", "subject": "One-1.html", "field": "x", "status": "FAIL", "detail": ""},
    ]}))
    assert st.failed_urls(_run) == [
        "https://www.fragrantica.com/perfume/B/Two-2.html", "https://www.fragrantica.com/perfume/C/Three-3.html"]
print("--only-failed finds the failed pages OK")

# perfumers: stored against the people the page links
assert so.compare_perfumers([{"slug": "A_B", "name": "A B"}], {"perfumers": [{"slug": "A_B", "name": "A B"}]})[0]["status"] == so.PASS
assert so.compare_perfumers([], {"perfumers": [{"slug": "A_B", "name": "A B"}]})[0]["status"] == so.FAIL
assert so.compare_perfumers([], {"perfumers": []})[0]["status"] == so.UNAVAILABLE
print("perfumer comparison OK")

# --random picks a repeatable sample of the manifest
with _tempfile.TemporaryDirectory() as _tmp:
    _manifest = _Path(_tmp) / "m.txt"
    _manifest.write_text("\n".join("https://www.fragrantica.com/perfume/B/P{0}-{0}.html".format(i) for i in range(1, 101)) + "\n")
    _a = st.random_urls(5, 42, _manifest)
    assert _a == st.random_urls(5, 42, _manifest) and len(set(_a)) == 5 and _a != st.random_urls(5, 43, _manifest)
    assert len(st.random_urls(500, 1, _manifest)) == 100
print("--random samples are repeatable OK")
