"""Run with: python tests/test_manifest.py  (needs lxml; no browser)."""
import os
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

import browser_client as bc  # noqa: E402
import perfumes_extract_manifest as em  # noqa: E402

B = "https://www.fragrantica.com"

# ---- link matching ----
tree = html.fromstring(
    '<a href="/perfume/Dior/Sauvage-31861.html?x=1">a</a>'
    '<a href="https://www.fragrantica.com/perfume/Dior/Sauvage-31861.html">dup</a>'
    '<a href="https://fragrantica.com/perfume/Chanel/No-5-531.html#r">bare host</a>'
    '<a href="../perfume/Gucci/Guilty-1.html">path-relative</a>'
    '<a href="https://evil.example/perfume/X/Y-9.html">other site</a>'
    '<a href="/perfume/Dior/">no id</a>'
)
assert em.matching_urls(tree, B + "/designers/Dior.html", em.PERFUME_PATH) == [
    B + "/perfume/Dior/Sauvage-31861.html", B + "/perfume/Chanel/No-5-531.html",
    B + "/perfume/Gucci/Guilty-1.html"]
print("link matching OK")


# ---- count-based scrolling ----
class Loc:
    def __init__(s, p): s.p = p
    def count(s): return s.p.count()

class Page:
    url = "http://x/"
    def __init__(s, growth):
        s.growth, s.n, s.scrolls = growth, 0, 0
    def locator(s, sel): return Loc(s)
    def count(s): return s.growth(s.scrolls)
    def evaluate(s, js):
        if "scrollBy" in js:
            s.scrolls += 1
        return True            # always "at bottom"
    def wait_for_timeout(s, ms): pass

client = bc.BrowserClient.__new__(bc.BrowserClient)
client.page = Page(lambda n: min(10 * n, 100))            # grows until 100 items, then stops
assert client._scroll_until_stable("a") == 100
assert client.page.scrolls <= 10 + bc.STABLE_SCROLL_CHECKS + 1
client.page = Page(lambda n: 10 * n)                      # never stops growing
bc.MAX_SCROLL_STEPS = 12
try:
    client._scroll_until_stable("a")
    raise AssertionError("expected ScrollIncompleteError")
except bc.ScrollIncompleteError:
    pass
print("scrolling stops when the count is stable and refuses a partial list OK")


# ---- crawl, checkpoint and empty designers ----
def page(links):
    return html.fromstring("<html><body>{}</body></html>".format(
        "".join('<a href="{}">x</a>'.format(h) for h in links)))

class Fake:
    def __init__(s, pages, fail_on=None):
        s.pages, s.fail_on, s.seen = pages, fail_on, []
    class page_:
        @staticmethod
        def title(): return "Designer"
    page = page_
    def fetch_tree(s, url, wait_for=None, scroll_for=None):
        s.seen.append(url)
        if url == s.fail_on:
            raise RuntimeError("HTTP 403")
        return s.pages[url]

def run(client):
    class FB:
        def __enter__(s): return client
        def __exit__(s, *a): return False
    em.BrowserClient = FB
    return em.main()

def d(name): return "/designers/{}.html".format(name)
def p(brand, i): return "/perfume/{}/P{}-{}.html".format(brand, i, i)
country = B + "/country/TestLand.html"
em.COUNTRIES = ["TestLand"]
em.migrate_legacy_manifest = lambda: None

pages = {country: page([d("A"), d("B"), d("C")]),
         B + d("A"): page([p("A", 1), p("A", 2)]),
         B + d("B"): page([p("B", 3)]),
         B + d("C"): page([p("C", 4)])}
with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    assert run(Fake(pages, fail_on=B + d("B"))) == 1
    saved = Path("perfumes_manifest.state.json").read_text()
    assert d("B") in saved, saved                       # position is the designer URL
    # on the next run a new designer appears earlier in the list; resume must still land on B
    pages[country] = page([d("X"), d("A"), d("B"), d("C")])
    pages[B + d("X")] = page([p("X", 9)])
    second = Fake(pages)
    assert run(second) == 0
    assert second.seen == [country, B + d("B"), B + d("C")], second.seen
    assert len(Path("perfumes_manifest.txt").read_text().split()) == 4
    print("crash -> resume by designer URL even though the list changed OK")

with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    pages = {country: page([d("A"), d("B")]), B + d("A"): page([]), B + d("B"): page([p("B", 3)])}
    c = Fake(pages)
    assert run(c) == 0
    assert c.seen.count(B + d("A")) == 2, "an empty designer is fetched twice"
    assert Path("perfumes_manifest.empty.txt").read_text().split() == [B + d("A")]
    assert Path("perfumes_manifest.txt").read_text().split() == [B + p("B", 3)]
    print("empty designer retried once, recorded, crawl continues OK")

with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    Path("perfumes_manifest.state.json").write_text('{"country_index": 0, "brand_url": "%s"}' % (B + d("B")))
    assert run(Fake({})) == 1                            # state without the .tmp manifest
    Path("perfumes_manifest.txt.tmp").write_text("")
    Path("perfumes_manifest.state.json").write_text('{"country_index": 0, "brand_url": "%s"}' % (B + d("GONE")))
    c = Fake({country: page([d("A")]), B + d("A"): page([p("A", 1)])})
    assert run(c) == 1 and c.seen == [country]           # saved designer vanished -> stop, don't guess
    print("inconsistent checkpoints refused OK")
