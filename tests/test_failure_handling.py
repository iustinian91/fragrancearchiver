"""Offline checks that things fail safely: bad images, blank or incomplete pages, challenge pages, a failed app.db build."""
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

from lxml import html

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
try:
    import patchright.sync_api  # noqa: F401
except ImportError:  # the checks below never open a browser
    stub = types.ModuleType("patchright.sync_api")
    stub.Error, stub.sync_playwright = type("Error", (Exception,), {}), None
    sys.modules["patchright"], sys.modules["patchright.sync_api"] = types.ModuleType("patchright"), stub

import browser_client as bc  # noqa: E402
import perfumes_scraper as ps  # noqa: E402

bc.sleep = lambda s: None
bc.random.uniform = lambda a, b: 0.0
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 2000


class Response:
    def __init__(self, status, body=b"", headers=None):
        self.status, self._body, self.headers = status, body, headers or {}
        self.ok = 200 <= status < 300

    def body(self):
        return self._body

    def dispose(self):
        pass


class Context:
    def __init__(self, responses):
        self.responses = list(responses)
        self.request = self

    def get(self, url, timeout=None):
        return self.responses.pop(0)


def client_with(*responses):
    client = bc.BrowserClient.__new__(bc.BrowserClient)
    client.timeout_ms = 1000
    client.context = Context(responses)
    return client


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    # a good image is written, and no temporary file is left behind
    assert client_with(Response(200, JPEG)).download_image("u", tmp / "a.jpg") is True
    assert (tmp / "a.jpg").read_bytes() == JPEG and not list(tmp.glob("*.part"))
    # a missing picture is skipped, not fatal
    assert client_with(Response(404)).download_image("u", tmp / "b.jpg") is False
    assert not (tmp / "b.jpg").exists()
    # empty and non-image bodies are refused and leave nothing on disk
    for body in (b"", b"<html>blocked</html>"):
        try:
            client_with(Response(200, body)).download_image("u", tmp / "c.jpg")
        except RuntimeError as exc:
            assert "Bad image" in str(exc)
        else:
            raise AssertionError("bad image accepted")
        assert not (tmp / "c.jpg").exists() and not list(tmp.glob("*.part"))
    # other HTTP errors still stop the crawl
    try:
        client_with(Response(403)).download_image("u", tmp / "d.jpg")
    except RuntimeError:
        pass
    else:
        raise AssertionError("403 ignored")
print("image downloads: atomic, validated, 404 skipped OK")

page = html.fromstring("<html><head><title>X</title></head><body><h1>X</h1></body></html>")
ps.check_page(page, "u")
for bad in ("<html><body></body></html>", "<html><head><title>T</title></head><body></body></html>"):
    try:
        ps.check_page(html.fromstring(bad), "u")
    except ValueError:
        pass
    else:
        raise AssertionError("blank page accepted")
print("blank pages are refused before being marked complete OK")

import build_app_db  # noqa: E402

old = os.getcwd()
with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    try:
        with open("app.db", "w") as f:
            f.write("previous good build")
        with open("perfumes_clean.json", "w") as f:
            f.write('[{"id": "1"}]')   # missing fields: the build fails part way
        try:
            build_app_db.main()
        except KeyError:
            pass
        else:
            raise AssertionError("bad data accepted")
        assert Path("app.db").read_text() == "previous good build"
        with open("perfumes_clean.json", "w") as f:
            f.write('[{"id": "1", "brand": "B", "name": "N", "slug": "b-n", "notes": {"general": ["Milk"]}, "perfumers": [{"name": "A B", "slug": "A_B"}]}]')
        build_app_db.main()
        assert sqlite3.connect("app.db").execute("select count(*) from perfumes").fetchone()[0] == 1
        assert sqlite3.connect("app.db").execute("select kind, note from notes").fetchall() == [("general", "Milk")]
        assert sqlite3.connect("app.db").execute("select name, slug, rank from perfumers").fetchall() == [("A B", "A_B", 1)]
        assert not Path("app.db.building").exists()
    finally:
        os.chdir(old)
print("failed app.db build keeps the previous database OK")

# the brand as the heading spells it, when the URL still carries an older brand name
moved = html.fromstring(
    "<html><head><title>T</title></head><body><nav><a href='/designers/Dior.html'>Dior</a></nav>"
    "<h1> Made in Heaven What We Do Is Secret <small>for women</small></h1>"
    "<a href='/designers/What-We-Do-Is-Secret.html'>What We Do Is Secret</a>"
    "<a href='/designers/'>Designers</a></body></html>"
)
assert ps.heading_brand(moved) == "What We Do Is Secret"
assert ps.heading_brand(html.fromstring("<html><body><h1>X</h1></body></html>")) is None
print("brand taken from the page heading when the URL is out of date OK")

# a Cloudflare interstitial that clears on its own is waited out; one that does not still stops the crawl
_real_reason = bc.cloudflare_challenge_reason
class _Waiter:
    def wait_for_timeout(self, ms):
        pass
_client = bc.BrowserClient.__new__(bc.BrowserClient)
_client.page = _Waiter()
for _clears_after, _expected in ((3, True), (10 ** 6, False)):
    _calls = {"n": 0}
    def _reason(page, _calls=_calls, _clears_after=_clears_after):
        _calls["n"] += 1
        return None if _calls["n"] >= _clears_after else "challenge"
    bc.cloudflare_challenge_reason = _reason
    assert _client._challenge_clears() is _expected
bc.cloudflare_challenge_reason = _real_reason
print("challenge pages that clear themselves are waited out, others still stop OK")

# a page whose vote cards had not all appeared is not recorded as complete, and the crawl goes on
_CARD = "<div class='tw-perf-card'><span class='uppercase'>{}</span><div class='space-y-2'><div>x</div><span>5</span></div></div>"


def _page(cards):
    return html.fromstring("<html><head><title>T</title></head><body><h1>Vanilla</h1>{}</body></html>".format(
        "".join(_CARD.format(name) for name in cards)))


class _Fake:
    def __init__(self):
        self.seen = []

    def fetch_tree(self, url, **kwargs):
        self.seen.append(url)
        return _page(("LONGEVITY", "SILLAGE") if url.endswith("-1.html") else ("LONGEVITY", "SILLAGE", "GENDER"))

    def download_image(self, image_url, destination):
        return True


class _Browser:
    def __enter__(self):
        return _Fake()

    def __exit__(self, *exc):
        return False


_old = os.getcwd()
with tempfile.TemporaryDirectory() as _tmp:
    os.chdir(_tmp)
    try:
        Path("perfumes_manifest.txt").write_text(
            "https://www.fragrantica.com/perfume/B/One-1.html\nhttps://www.fragrantica.com/perfume/B/Two-2.html\n")
        _real = ps.BrowserClient
        ps.BrowserClient = _Browser
        try:
            assert ps.main(["--db", "p.sqlite"]) == 0
        finally:
            ps.BrowserClient = _real
        _rows = sqlite3.connect("p.sqlite").execute("select count(*) from perfumes").fetchone()[0]
        assert _rows == 1, _rows   # only the complete page was recorded
        assert "One-1.html" in Path("perfumes_incomplete.txt").read_text()
        assert "gender card missing" in Path("perfumes_incomplete.txt").read_text()
        assert Path("perfumes_incomplete/One-1.html").is_file()   # the page is kept for diagnosis
    finally:
        os.chdir(_old)
print("pages with missing vote cards are skipped, listed and retried later OK")

# ---- politeness: slow down after a block, stop after three in a row ----
import types as _types  # noqa: E402

_slept = []
_real_sleep, bc.sleep = bc.sleep, lambda s: _slept.append(s)
_real_uniform, bc.random.uniform = bc.random.uniform, lambda low, high: low   # earlier tests zero the delays
_real_uniform, bc.random.uniform = bc.random.uniform, lambda a, b: a   # the file stubs it to 0


def _fresh():
    c = bc.BrowserClient.__new__(bc.BrowserClient)
    c.slowdown, c.block_streak, c.clean_streak = 1.0, 0, 0
    return c


try:
    c = _fresh()
    c._pace_request((4.0, 4.0))
    c._note_block("test")
    c._pace_request((4.0, 4.0))
    c._note_block("test")
    c._pace_request((4.0, 4.0))
    assert _slept == [4.0, 8.0, 16.0], _slept
    c.block_streak = 0
    c = _fresh()
    c.slowdown = 100.0
    c._note_block("x")
    assert c.slowdown == bc.MAX_SLOWDOWN, "the slowdown has a ceiling"

    # three blocks in a row stop the run; a fetch that goes through ends the streak
    c = _fresh()
    c._note_block("a")
    c._note_block("b")
    c._note_ok()
    c._note_block("c")
    c._note_block("d")
    try:
        c._note_block("e")
        raise AssertionError("three in a row must stop")
    except bc.TooManyBlocksError as exc:
        assert "3 requests in a row" in str(exc) and "rerun" in str(exc)
    assert issubclass(bc.TooManyBlocksError, RuntimeError) and issubclass(bc.TooManyBlocksError, bc.FETCH_ERRORS)

    # a long clean stretch halves the slowdown, one step at a time
    c = _fresh()
    c.slowdown = 4.0
    for _ in range(bc.CLEAN_FETCHES_TO_SPEED_UP - 1):
        c._note_ok()
    assert c.slowdown == 4.0
    c._note_ok()
    assert c.slowdown == 2.0 and c.clean_streak == 0

    # fetch_tree: repeated 429s end with TooManyBlocksError, not five retries
    class Page429:
        url = "https://www.fragrantica.com/x"

        def goto(self, *a, **k):
            return _types.SimpleNamespace(status=429, headers={})

    c = _fresh()
    c.page, c.timeout_ms = Page429(), 1000
    c._rate_limit_wait = lambda *a, **k: None
    try:
        c.fetch_tree("https://www.fragrantica.com/x")
        raise AssertionError("expected a stop")
    except bc.TooManyBlocksError:
        pass
    assert c.block_streak == bc.MAX_CONSECUTIVE_BLOCKS and c.slowdown == 8.0
finally:
    bc.sleep, bc.random.uniform = _real_sleep, _real_uniform

# the profile options: a caller's earlier choice wins, otherwise --profile / --no-profile decide
import argparse  # noqa: E402
_p = argparse.ArgumentParser()
bc.add_browser_arguments(_p)
_old = bc.PROFILE_DIR, bc.BLOCK_ADS
try:
    bc.PROFILE_DIR = None
    bc.apply_browser_arguments(_p.parse_args([]))
    assert bc.PROFILE_DIR == "browser_profile"
    bc.PROFILE_DIR = None
    bc.apply_browser_arguments(_p.parse_args(["--no-profile"]))
    assert bc.PROFILE_DIR == ""
    bc.PROFILE_DIR = ""
    bc.apply_browser_arguments(_p.parse_args([]))
    assert bc.PROFILE_DIR == "", "the smoke test's fresh browser is kept"
finally:
    bc.PROFILE_DIR, bc.BLOCK_ADS = _old
print("slowdown, stop after three blocks, and profile options OK")
