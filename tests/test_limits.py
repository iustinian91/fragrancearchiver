"""Offline checks for the session limits (--max-pages, --max-hours) and the automatic rest after a block
(--rest-after-block). No browser, no network: python tests/test_limits.py"""
import argparse
import contextlib
import io
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
sys.dont_write_bytecode = True
try:
    import patchright.sync_api  # noqa: F401
except ImportError:  # nothing below opens a browser
    stub = types.ModuleType("patchright.sync_api")
    stub.Error, stub.sync_playwright = type("Error", (Exception,), {}), None
    sys.modules["patchright"], sys.modules["patchright.sync_api"] = types.ModuleType("patchright"), stub

from archiver import browser_client as bc  # noqa: E402
import notes_scraper as ns  # noqa: E402
import perfumes_extract_manifest as em  # noqa: E402
import perfumes_scraper as ps  # noqa: E402

slept = []
clock = [1000.0]


def fake_sleep(seconds):
    slept.append(seconds)
    clock[0] += seconds          # a rest takes (fake) time, so --max-hours sees it


bc.sleep = fake_sleep
bc.random.uniform = lambda a, b: 0.0
bc.monotonic = lambda: clock[0]


def client(**settings):
    c = bc.BrowserClient.__new__(bc.BrowserClient)      # as the other tests do: no browser
    c.slowdown, c.block_streak, c.clean_streak = 1.0, 0, 0
    for name, value in settings.items():
        setattr(c, name, value)
    return c


# ---- the options: defaults, validation, and what they set ----
parser = argparse.ArgumentParser()
bc.add_browser_arguments(parser)
args = parser.parse_args([])
assert args.max_hours is None and args.max_pages is None and args.rest_after_block == 60, args
args = parser.parse_args(["--max-hours", "5.5", "--max-pages", "400", "--rest-after-block", "90"])
assert (args.max_hours, args.max_pages, args.rest_after_block) == (5.5, 400, 90)
for bad in (["--max-pages", "0"], ["--max-hours", "0"], ["--max-hours", "-1"], ["--rest-after-block", "-5"],
            ["--max-pages", "x"]):
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parser.parse_args(bad)
            raise AssertionError("{} must be refused".format(bad))
        except SystemExit as exit_:
            assert exit_.code == 2

saved = bc.SESSION_MAX_SECONDS, bc.SESSION_MAX_PAGES, bc.BLOCK_REST_SECONDS, bc.PROFILE_DIR, bc.BLOCK_ADS
try:
    bc.PROFILE_DIR = ""
    bc.apply_browser_arguments(args)
    assert (bc.SESSION_MAX_SECONDS, bc.SESSION_MAX_PAGES, bc.BLOCK_REST_SECONDS) == (19800.0, 400, 5400.0)
    made = bc.BrowserClient()
    assert (made.max_seconds, made.max_pages, made.rest_seconds) == (19800.0, 400, 5400.0)
    bc.apply_browser_arguments(parser.parse_args([]))
    assert bc.SESSION_MAX_SECONDS is None and bc.SESSION_MAX_PAGES is None and bc.BLOCK_REST_SECONDS == 3600.0
finally:
    bc.SESSION_MAX_SECONDS, bc.SESSION_MAX_PAGES, bc.BLOCK_REST_SECONDS, bc.PROFILE_DIR, bc.BLOCK_ADS = saved
# a client built without the options (the dry test, the other tests) has no limit and no rest
plain = client()
assert plain.max_pages is None and plain.max_seconds is None and plain.rest_seconds == 0
print("options: defaults, validation and the settings they make OK")

# ---- --max-pages ----
c = client(max_pages=3)
c._fetch_tree = lambda *a, **k: "tree"
assert [c.fetch_tree("u") for _ in range(3)] == ["tree"] * 3
try:
    c.fetch_tree("u")
    raise AssertionError("the 4th page must be refused")
except bc.SessionLimitReached as exc:
    assert "3 pages (--max-pages)" in str(exc) and "after 3 page(s)" in str(exc), exc
assert not issubclass(bc.SessionLimitReached, bc.FETCH_ERRORS), "the FETCH_ERRORS handlers must not swallow it"
assert c.pages_started == 3
c = client()                                              # no limit: any number of pages
c._fetch_tree = lambda *a, **k: "tree"
for _ in range(50):
    c.fetch_tree("u")
print("--max-pages stops before page N+1 and is not an ordinary fetch error OK")

# ---- --max-hours: counted from the start, rests included; no new page once it is up ----
clock[0] = 1000.0
c = client(max_seconds=2 * 3600)
c._fetch_tree = lambda *a, **k: "tree"
c.fetch_tree("u")                                         # starts the clock
clock[0] += 2 * 3600 - 1
c.fetch_tree("u")                                         # one second left: still allowed
clock[0] += 1
try:
    c.fetch_tree("u")
    raise AssertionError("the time limit must stop a new page")
except bc.SessionLimitReached as exc:
    assert "2 hours (--max-hours)" in str(exc), exc
print("--max-hours stops new pages once the time is up OK")

# ---- the rest after a block ----
blocked = bc.TooManyBlocksError("the site blocked 3 requests in a row (HTTP 429 loading x); stopping. Progress is saved")


def blocks_then_ok(times):
    state = {"left": times}

    def fetch(*a, **k):
        if state["left"]:
            state["left"] -= 1
            raise blocked
        return "tree"
    return fetch


slept.clear()
out = io.StringIO()
c = client(rest_seconds=600, block_streak=3)
c.slowdown, c.clean_streak = 4.0, 7
c._recent().extend([True] * 5)
c._fetch_tree = blocks_then_ok(1)
with contextlib.redirect_stdout(out):
    assert c.fetch_tree("u") == "tree"
assert slept == [600], slept                                # one 10 minute rest, then the same page again
assert c.block_streak == 0 and not any(c._recent()) and c.rests_taken == 1
assert c.slowdown == 4.0, "the slowdown fades by itself after clean fetches; the rest does not reset it"
assert "Resting 10 minutes (automatic rest 1 of 3)" in out.getvalue() and "stopping" not in out.getvalue(), out.getvalue()

slept.clear()                                               # the limit of rests per run
c = client(rest_seconds=600)
c._fetch_tree = blocks_then_ok(99)
with contextlib.redirect_stdout(io.StringIO()):
    try:
        c.fetch_tree("u")
        raise AssertionError("after the last rest the block must stop the run")
    except bc.TooManyBlocksError:
        pass
assert slept == [600] * bc.MAX_BLOCK_RESTS, slept
c._fetch_tree = lambda *a, **k: "tree"
assert c.fetch_tree("u") == "tree"

slept.clear()                                               # no rest configured: the old behaviour, a stop
c = client()
c._fetch_tree = blocks_then_ok(1)
try:
    c.fetch_tree("u")
    raise AssertionError("without --rest-after-block a block ends the run")
except bc.TooManyBlocksError:
    pass
assert slept == []

slept.clear()                                               # a rest that would outlast --max-hours is not taken
clock[0] = 5000.0
c = client(rest_seconds=3600, max_seconds=3600)
c._fetch_tree = blocks_then_ok(1)
c._session_start = clock[0]
clock[0] += 1800                                            # 30 minutes in, 30 left; the rest is 60
try:
    c.fetch_tree("u")
    raise AssertionError("expected a stop")
except bc.SessionLimitReached as exc:
    assert "would go past the time limit" in str(exc), exc
assert slept == []

slept.clear()                                               # a rest that still fits is taken, and the page then loads
clock[0] = 5000.0
c = client(rest_seconds=600, max_seconds=3600)
c._session_start = clock[0]
fetches = []


def fetch_once(*a, **k):
    fetches.append(1)
    if len(fetches) == 1:
        clock[0] += 2900                                    # 700 seconds left: the 600 second rest fits
        raise blocked
    return "tree"


c._fetch_tree = fetch_once
with contextlib.redirect_stdout(io.StringIO()):
    assert c.fetch_tree("u") == "tree"
assert slept == [600] and len(fetches) == 2

# an image download rests the same way, and is not counted as a page
slept.clear()
c = client(rest_seconds=120, max_pages=1)
c._download_image = blocks_then_ok(1)
with contextlib.redirect_stdout(io.StringIO()):
    assert c.download_image("i", "d") == "tree"
assert slept == [120] and c.pages_started == 0
print("the rest after a block: taken, limited to {} per run, off by default, never past --max-hours OK".format(
    bc.MAX_BLOCK_RESTS))


# ---- the scrapers and the crawler stop cleanly at a limit and a rerun carries on ----
class Limited:
    """A stand-in browser that refuses the page after `allow` fetches, like BrowserClient at --max-pages."""
    def __init__(self, allow, page_for):
        self.allow, self.page_for, self.fetched = allow, page_for, 0

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def fetch_tree(self, url, **kwargs):
        if self.allow is not None and self.fetched >= self.allow:
            raise bc.SessionLimitReached("reached the limit of {} pages (--max-pages) after {} page(s)".format(
                self.allow, self.fetched))
        self.fetched += 1
        return self.page_for(url)

    def download_image(self, image_url, destination):
        return False


CARD = "<div class='tw-perf-card'><span class='uppercase'>{}</span><div class='space-y-2'><div>x</div><span>5</span></div></div>"
perfume_page = html.fromstring("<html><head><title>T</title></head><body><h1>Vanilla</h1>{}</body></html>".format(
    "".join(CARD.format(name) for name in ("LONGEVITY", "SILLAGE", "GENDER"))))

old = os.getcwd()
with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    try:
        Path("perfumes_manifest.txt").write_text("".join(
            "https://www.fragrantica.com/perfume/B/N{0}-{0}.html\n".format(i) for i in range(1, 6)))
        real = ps.BrowserClient
        runs = []
        try:
            for allow in (2, 2, None):
                ps.BrowserClient = lambda allow=allow: Limited(allow, lambda url: perfume_page)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    runs.append((ps.main(["--db", "p.sqlite"]), out.getvalue()))
        finally:
            ps.BrowserClient = real
        count = sqlite3.connect("p.sqlite").execute("select count(*) from perfumes").fetchone()[0]
        assert [code for code, _ in runs] == [bc.EXIT_SESSION_LIMIT, bc.EXIT_SESSION_LIMIT, 0], runs
        assert "Stopped: reached the limit of 2 pages" in runs[0][1] and "rerun to continue" in runs[0][1], runs[0][1]
        assert "2 URLs are recorded as complete" in runs[0][1] and "4 URLs are recorded as complete" in runs[1][1]
        assert "Completed: 5 perfume URLs" in runs[2][1] and count == 5, (count, runs[2][1])
        assert "Scrape paused" not in "".join(text for _, text in runs)
    finally:
        os.chdir(old)
print("perfumes_scraper: stops at the limit with exit status 3, keeps its rows, the rerun finishes OK")

BERGAMOT = "https://www.fragrantica.com/notes/Bergamot-75.html"
AMBER = "https://www.fragrantica.com/notes/Amber-9.html"
note_page = html.fromstring((ROOT / "tests/fixtures/note_bergamot.html").read_text(encoding="utf-8"))
with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    try:
        ns.IMAGE_DIR = Path("images")
        ns.CATEGORIES_FILE, ns.GONE_FILE, ns.FAILED_FILE = Path("c.json"), Path("g.txt"), Path("f.txt")
        Path("notes_manifest.txt").write_text(BERGAMOT + "\n" + AMBER + "\n")
        real = ns.BrowserClient
        try:
            codes = []
            for allow in (1, None):
                ns.BrowserClient = lambda allow=allow: Limited(allow, lambda url: note_page)
                with contextlib.redirect_stdout(io.StringIO()):
                    codes.append(ns.main(["--db", "n.sqlite"]))
        finally:
            ns.BrowserClient = real
        assert codes == [bc.EXIT_SESSION_LIMIT, 0], codes
        assert sqlite3.connect("n.sqlite").execute("select count(*) from notes").fetchone()[0] == 2
    finally:
        os.chdir(old)
print("notes_scraper: stops at the limit with exit status 3, the rerun finishes OK")

B = em.BASE_URL
country = B + "/country/TestLand.html"
em.COUNTRIES = ["TestLand"]


def listing(links):
    return html.fromstring("<html><body>{}</body></html>".format("".join('<a href="{}">x</a>'.format(h) for h in links)))


pages = {country: listing(["/designers/A.html", "/designers/B.html", "/designers/C.html"]),
         B + "/designers/A.html": listing(["/perfume/A/P1-1.html"]),
         B + "/designers/B.html": listing(["/perfume/B/P2-2.html"]),
         B + "/designers/C.html": listing(["/perfume/C/P3-3.html"])}
with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    try:
        real = em.BrowserClient
        try:
            codes = []
            for allow in (2, 2, None):           # country page + 1 designer, then the country page + 1 more, then the rest
                em.BrowserClient = lambda allow=allow: Limited(allow, lambda url: pages[url])
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    codes.append(em.main([]))
                if codes[-1] == bc.EXIT_SESSION_LIMIT:
                    assert "Stopped: reached the limit" in out.getvalue() and "rerun to continue" in out.getvalue()
                    assert Path("perfumes_manifest.txt.tmp").exists() and Path("perfumes_manifest.state.json").exists()
                    assert not Path("perfumes_manifest.txt").exists(), "an unfinished crawl must not become the manifest"
        finally:
            em.BrowserClient = real
        assert codes == [bc.EXIT_SESSION_LIMIT, bc.EXIT_SESSION_LIMIT, 0], codes
        assert len(Path("perfumes_manifest.txt").read_text().split()) == 3
    finally:
        os.chdir(old)
print("perfumes_extract_manifest: stops at the limit, keeps its place, the reruns finish the crawl OK")
