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
from archiver import paths  # noqa: E402
paths.use_flat()   # these tests work in a scratch folder and expect plain file names
sys.dont_write_bytecode = True
try:
    import patchright.sync_api  # noqa: F401
except ImportError:  # the checks below never open a browser
    stub = types.ModuleType("patchright.sync_api")
    stub.Error, stub.sync_playwright = type("Error", (Exception,), {}), None
    sys.modules["patchright"], sys.modules["patchright.sync_api"] = types.ModuleType("patchright"), stub

from archiver import browser_client as bc  # noqa: E402
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
    # empty and non-image bodies are refused and leave nothing on disk; the crawl goes on
    for body in (b"", b"<html>blocked</html>"):
        assert client_with(Response(200, body)).download_image("u", tmp / "c.jpg") is False
        assert not (tmp / "c.jpg").exists() and not list(tmp.glob("*.part"))
    # a server error on one picture skips it too
    assert client_with(Response(500)).download_image("u", tmp / "e.jpg") is False
    # a 403 may be a block, so it still stops the crawl
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
            build_app_db.main([])
        except KeyError:
            pass
        else:
            raise AssertionError("bad data accepted")
        assert Path("app.db").read_text() == "previous good build"
        with open("perfumes_clean.json", "w") as f:
            f.write('[{"id": "1", "brand": "B", "name": "N", "slug": "b-n", "notes": {"general": ["Milk"]}, "perfumers": [{"name": "A B", "slug": "A_B"}]}]')
        build_app_db.main([])
        assert sqlite3.connect("app.db").execute("select count(*) from perfumes").fetchone()[0] == 1
        assert sqlite3.connect("app.db").execute("select kind, note from notes").fetchall() == [("general", "Milk")]
        assert sqlite3.connect("app.db").execute("select name, slug, rank from perfumers").fetchall() == [("A B", "A_B", 1)]
        assert not Path("app.db.building").exists()
    finally:
        os.chdir(old)

# --in/--out, and a build that fails its own checks is not swapped in
old = os.getcwd()
with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    try:
        Path("other.json").write_text('[{"id": "7", "brand": "B", "name": "N", "slug": "b-n"}]')
        build_app_db.main(["--in", "other.json", "--out", "custom.db"])
        assert sqlite3.connect("custom.db").execute("select id from perfumes").fetchone() == ("7",)
        Path("custom.db").write_text("previous good build")
        real_check, build_app_db.check_build = build_app_db.check_build, lambda *a: ["perfumes: 0 rows, expected 1"]
        try:
            build_app_db.main(["--in", "other.json", "--out", "custom.db"])
            raise AssertionError("a failed check must stop the build")
        except SystemExit as exc:
            assert "did not pass its checks" in str(exc)
        finally:
            build_app_db.check_build = real_check
        assert Path("custom.db").read_text() == "previous good build" and not Path("custom.db.building").exists()
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

    # page requests: the time a page took counts toward the wait before the next one
    clock = [100.0]
    _real_monotonic, bc.monotonic = bc.monotonic, lambda: clock[0]
    _slept.clear()
    c = _fresh()
    c._pace_request((6.0, 6.0), page=True)
    assert _slept == [6.0], _slept                 # the first page waits the full time
    clock[0] += 14.0                               # reading the page took 14 s
    c._pace_request((6.0, 6.0), page=True)
    assert _slept[-1] == 0.0, _slept               # so nothing more is needed
    clock[0] += 2.0                                # a quick page (an error, a skipped one)
    c._pace_request((6.0, 6.0), page=True)
    assert _slept[-1] == 4.0, _slept               # still gets the rest of the gap
    c.slowdown = 4.0                               # after blocks the wait grows 4x ...
    clock[0] += 14.0
    c._pace_request((6.0, 6.0), page=True)
    assert _slept[-1] == 10.0, _slept              # ... so a 14 s page is followed by 10 s more
    _slept.clear()
    c._pace_request((4.0, 4.0))                    # non-page requests (images) keep their plain pause
    assert _slept == [16.0], _slept
    bc.monotonic = _real_monotonic
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
    assert bc.PROFILE_DIR == "", "the dry test's fresh browser is kept"
    # ad blocking: on by default, switched off by --no-adblock, and a caller's choice is kept
    bc.BLOCK_ADS = True
    bc.apply_browser_arguments(_p.parse_args([]))
    assert bc.BLOCK_ADS is True
    bc.apply_browser_arguments(_p.parse_args(["--no-adblock"]))
    assert bc.BLOCK_ADS is False
    bc.apply_browser_arguments(_p.parse_args([]))
    assert bc.BLOCK_ADS is False, "no flag keeps what a caller set"
finally:
    bc.PROFILE_DIR, bc.BLOCK_ADS = _old
print("slowdown, stop after three blocks, and profile options OK")


# ---- pages that are gone, server errors, a huge Retry-After ----
class _Resp:
    def __init__(self, status, headers=None):
        self.status, self.headers = status, headers or {}


class _GotoPage:
    url = "https://www.fragrantica.com/x"

    def __init__(self, statuses):
        self.statuses = list(statuses)

    def goto(self, *a, **k):
        return _Resp(self.statuses.pop(0))

    def title(self):
        return "ok"

    def content(self):
        return "<html><body>ok</body></html>"

    def locator(self, *a):
        raise AssertionError("not needed")


def _client(statuses):
    c = bc.BrowserClient.__new__(bc.BrowserClient)
    c.slowdown, c.block_streak, c.clean_streak, c.timeout_ms = 1.0, 0, 0, 1000
    c.page = _GotoPage(statuses)
    return c


_real_reason = bc.cloudflare_challenge_reason
bc.cloudflare_challenge_reason = lambda page: None
try:
    for status in (404, 410):
        try:
            _client([status]).fetch_tree("https://www.fragrantica.com/x")
            raise AssertionError("a missing page must raise")
        except bc.PageGoneError as exc:
            assert str(status) in str(exc)
    assert issubclass(bc.PageGoneError, RuntimeError)
    c = _client([503, 502, 200])                      # two server errors, then the page
    assert c.fetch_tree("https://www.fragrantica.com/x") is not None and c.page.statuses == []
    try:
        _client([500, 500, 500]).fetch_tree("https://www.fragrantica.com/x")
        raise AssertionError("a lasting server error must raise")
    except RuntimeError as exc:
        assert "after 2 retries" in str(exc) and not isinstance(exc, bc.PageGoneError)
    try:
        _client([403]).fetch_tree("https://www.fragrantica.com/x")
        raise AssertionError("403 must raise")
    except RuntimeError as exc:
        assert "HTTP 403" in str(exc)
finally:
    bc.cloudflare_challenge_reason = _real_reason
try:
    bc.BrowserClient._rate_limit_wait("u", {"retry-after": "86400"}, "loading")
    raise AssertionError("a day-long wait must stop the run")
except bc.TooManyBlocksError as exc:
    assert "86400" in str(exc)
print("gone pages, server errors and an excessive Retry-After are handled OK")

# a list still growing at the scroll limit: returned as it is when allowed, an error otherwise
class _GrowingPage:
    def __init__(self):
        self.n = 0
    class _Loc:
        def __init__(s, page): s.page = page
        def count(s): return s.page.n
    def locator(self, sel): return _GrowingPage._Loc(self)
    def evaluate(self, js):
        if "scrollBy" in js:
            self.n += 10
        return True
    def wait_for_timeout(self, ms): pass
    url = "u"


_steps = bc.MAX_SCROLL_STEPS
bc.MAX_SCROLL_STEPS = 12
try:
    c = bc.BrowserClient.__new__(bc.BrowserClient)
    c.page = _GrowingPage()
    assert c._scroll_until_stable("a", allow_partial=True) == 110 and c.scroll_complete is False
    c.page = _GrowingPage()
    try:
        c._scroll_until_stable("a")
        raise AssertionError("expected ScrollIncompleteError")
    except bc.ScrollIncompleteError:
        pass
finally:
    bc.MAX_SCROLL_STEPS = _steps
assert bc.MAX_SCROLL_STEPS >= 600
print("a list still growing at the scroll limit can be kept as partial OK")

# ---- the scraper: gone and unreadable pages are skipped and listed; a run of unreadable ones stops ----
class _PageFake:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def fetch_tree(self, url, **kwargs):
        what = self.behaviour(url)
        if what == "gone":
            raise bc.PageGoneError("HTTP 404 for " + url)
        if what == "blank":
            return html.fromstring("<html><body></body></html>")
        if what == "incomplete":
            return _page(("LONGEVITY", "SILLAGE"))      # no gender card
        return _page(("LONGEVITY", "SILLAGE", "GENDER"))

    def download_image(self, image_url, destination):
        return False   # the picture is not available


def _crawl(urls, behaviour):
    old = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            Path("perfumes_manifest.txt").write_text("".join("https://www.fragrantica.com/perfume/B/N{0}-{0}.html\n".format(i) for i in urls))

            class Browser:
                def __enter__(s): return _PageFake(behaviour)
                def __exit__(s, *e): return False
            real, ps.BrowserClient = ps.BrowserClient, Browser
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = ps.main(["--db", "p.sqlite"])
                    code2 = ps.main(["--db", "p.sqlite"])      # a second run
            finally:
                ps.BrowserClient = real
            conn = sqlite3.connect("p.sqlite")
            return (code, code2, out.getvalue(), conn.execute("select count(*) from perfumes").fetchone()[0],
                    conn.execute("select count(*) from gone_urls").fetchone()[0],
                    Path("perfumes_gone.txt").read_text() if Path("perfumes_gone.txt").exists() else "",
                    Path("perfumes_failed.txt").read_text() if Path("perfumes_failed.txt").exists() else "",
                    conn.execute("select perfume from perfumes").fetchall())
        finally:
            os.chdir(old)


import contextlib  # noqa: E402
import io  # noqa: E402
code, code2, out, rows, gone_rows, gone_log, failed_log, perfumes = _crawl(
    [1, 2, 3], lambda url: "gone" if "N2-" in url else "ok")
assert code == 0 and rows == 2 and gone_rows == 1 and "N2-2.html" in gone_log, (code, rows, gone_rows, gone_log)
assert "will not be asked for again" in out
assert '"image": null' in perfumes[0][0] or '"image": null' in str(perfumes), "a missing picture is not named"
assert len(gone_log.splitlines()) == 1, "the gone page is not asked for again on the next run"
code, code2, out, rows, *_ = _crawl([1, 2], lambda url: "blank" if "N1-" in url else "ok")
assert code == 0 and rows == 1                                           # one unreadable page is skipped
code, code2, out, rows, gone_rows, gone_log, failed_log, _ = _crawl([1, 2, 3, 4, 5, 6], lambda url: "blank")
assert code == 1 and "5 pages in a row could not be read" in out and failed_log.count("\n") >= 5
print("gone and unreadable pages are skipped and listed; a run of them stops the crawl OK")


# ---- a page that stays incomplete is tried 3 times in all, logged once, then left alone ----
def _runs(behaviour, runs, extra=()):
    old, tries = os.getcwd(), []
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            Path("perfumes_manifest.txt").write_text("".join(
                "https://www.fragrantica.com/perfume/B/N{0}-{0}.html\n".format(i) for i in (1, 2, 3)))

            def seen(url):
                tries.append(url)
                return behaviour(url)

            class Browser:
                def __enter__(s): return _PageFake(seen)
                def __exit__(s, *e): return False
            real, ps.BrowserClient = ps.BrowserClient, Browser
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    for argv in runs:
                        ps.main(["--db", "p.sqlite"] + list(argv))
            finally:
                ps.BrowserClient = real
            return tries, Path("perfumes_incomplete.txt").read_text()
        finally:
            os.chdir(old)


tries, log = _runs(lambda url: "incomplete" if "N2-" in url else "ok", [()] * 5)
assert sum("N2-2" in t for t in tries) == ps.MAX_SKIP_ATTEMPTS == 3, tries
assert len(log.splitlines()) == 1, "one log line per page, not one per try"
tries, log = _runs(lambda url: "incomplete" if "N2-" in url else "ok", [()] * 4 + [("--retry-skipped",)])
assert sum("N2-2" in t for t in tries) == 4, "--retry-skipped gives a skipped page another go"
print("incomplete pages are retried 3 times, logged once, and --retry-skipped resets them OK")

# ---- repeated challenges that clear by themselves still stop the run (5 of the last 20 fetches) ----
c = bc.BrowserClient.__new__(bc.BrowserClient)
c.slowdown, c.block_streak, c.clean_streak = 1.0, 0, 0
try:
    for n in range(10):
        c._note_block("challenge cleared by itself")      # followed by a good fetch each time
        c._note_ok()
    raise AssertionError("expected a stop")
except bc.TooManyBlocksError as exc:
    assert "5 of the last" in str(exc), exc
c = bc.BrowserClient.__new__(bc.BrowserClient)
c.slowdown, c.block_streak, c.clean_streak = 1.0, 0, 0
for n in range(60):                                       # one block in every 10 fetches is fine
    if n % 10 == 0:
        c._note_block("x")
    c._note_ok()
print("a block in every few fetches stops the run; the occasional one does not OK")


# ---- the registration popup is hidden in every browser the client starts ----
from archiver import browser_client as _bc  # noqa: E402

assert "registration-nudge-overlay" in _bc.HIDE_POPUPS_JS and "display: none" in _bc.HIDE_POPUPS_JS


class _Ctx:
    def __init__(self):
        self.scripts, self.routes, self.pages = [], [], []

    def add_init_script(self, script=None, **kw):
        self.scripts.append(script)

    def route(self, pattern, handler):
        self.routes.append(pattern)

    def new_page(self):
        page = types.SimpleNamespace(set_default_timeout=lambda ms: None)
        self.pages.append(page)
        return page

    def close(self):
        pass


class _Chromium:
    def __init__(self, ctx):
        self.ctx = ctx

    def launch(self, **kw):
        return types.SimpleNamespace(new_context=lambda: self.ctx, close=lambda: None)

    def launch_persistent_context(self, folder, **kw):
        return self.ctx


for _profile in ("", "profile_dir_for_test"):
    for _ads in (True, False):
        _ctx = _Ctx()
        _real_pw = _bc.sync_playwright
        _bc.sync_playwright = lambda c=_ctx: types.SimpleNamespace(
            start=lambda: types.SimpleNamespace(chromium=_Chromium(c), stop=lambda: None))
        try:
            _client = _bc.BrowserClient(profile_dir=_profile, block_ads=_ads) if "profile_dir" in _bc.BrowserClient.__init__.__code__.co_varnames else None
            if _client is None:
                break
            with tempfile.TemporaryDirectory() as _t:
                os.chdir(_t)
                with _client:
                    pass
        finally:
            _bc.sync_playwright = _real_pw
        assert _ctx.scripts == [_bc.HIDE_POPUPS_JS], (_profile, _ads, _ctx.scripts)
        assert bool(_ctx.routes) == _ads        # popups are hidden whether or not ad blocking is on
print("the popup-hiding rule is installed with and without ad blocking and a saved profile OK")
