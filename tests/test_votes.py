"""Run with: python tests/test_votes.py  (needs lxml; patchright is not used)."""
import sys
import types
from pathlib import Path

from lxml import html

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from archiver import paths  # noqa: E402
paths.use_flat()   # these tests work in a scratch folder and expect plain file names
try:
    import patchright  # noqa: F401
except ImportError:  # only the import is needed here, not a browser
    stub = types.ModuleType("patchright.sync_api")
    stub.Error, stub.sync_playwright = Exception, None
    sys.modules["patchright"], sys.modules["patchright.sync_api"] = types.ModuleType("patchright"), stub

import perfumes_scraper as ps  # noqa: E402


def page_votes(tree):
    charts = ps.page_charts(tree)
    return charts["longevity"], charts["sillage"]


tree = html.fromstring((ROOT / "tests/fixtures/performance_section.html").read_text(encoding="utf-8"))
# a second real page, the cards as rendered in the browser (Black Orchid); it also has Gender
# and Price Value cards, which must be ignored
orchid = html.fromstring((ROOT / "tests/fixtures/performance_cards_black_orchid.html").read_text(encoding="utf-8"))
assert page_votes(orchid)[0]["very long lasting"] == "5300" and page_votes(orchid)[1]["enormous"] == "4000"
# the page as served has empty cards: that must read as "no votes yet", not as an error
served = html.fromstring('<div class="tw-perf-card"><div class="p-4"><longevity-rating-new/></div></div>')
assert page_votes(served) == (None, None)
longevity, sillage = page_votes(tree)
assert longevity == {"poor": "216", "weak": "403", "moderate": "2600", "long lasting": "6000",
                     "very long lasting": "5300"}, longevity
assert sillage == {"soft": "620", "moderate": "3600", "heavy": "6600", "enormous": "4000"}, sillage
assert ps.parse_count("1,234") == 1234 and ps.parse_count("1.2M") == 1200000 and ps.parse_count("x") is None
assert page_votes(html.fromstring("<html><body><p>nothing</p></body></html>")) == (None, None)
zero = html.fromstring(
    ps_html := (ROOT / "tests/fixtures/performance_section.html").read_text(encoding="utf-8")
)
for span in zero.xpath("//span[contains(@class,'text-[10px]')]"):
    span.text = "0"
zero_votes = page_votes(zero)
assert zero_votes[0] and not any(int(v) for v in zero_votes[0].values()), "no votes at all is stored as zeros"

# ---- scrolling for the vote cards stops at the Reviews heading ----
from archiver import browser_client as bc  # noqa: E402


class Loc:
    def __init__(self, page): self.page = page
    def count(self): return 0          # the cards never appear (a perfume with no votes)


class Page:
    def __init__(self, reviews_after): self.scrolls, self.reviews_after = 0, reviews_after
    def locator(self, selector): return Loc(self)
    def evaluate(self, js, *args):
        if "scrollBy" in js:
            self.scrolls += 1
            return None
        return self.scrolls >= self.reviews_after      # the Reviews heading check
    def wait_for_timeout(self, ms): pass


client = bc.BrowserClient.__new__(bc.BrowserClient)
client.page = Page(reviews_after=4)
client._wait_for_lazy(".tw-perf-card", stop_at="Reviews")
assert client.page.scrolls == 4, client.page.scrolls            # stopped at Reviews, not at the cap
client.page = Page(reviews_after=999)
client._wait_for_lazy(".tw-perf-card", stop_at="Reviews")
assert client.page.scrolls == bc.MAX_LAZY_STEPS                  # still bounded without it
print("scroll stops at the Reviews heading OK")
print("vote extraction OK")

# the line the scraper prints when a chart is missing says what the cards held
empty_cards = html.fromstring(
    "<html><body><div class='tw-perf-card'><span class='uppercase'>GENDER</span>"
    "<div class='space-y-2'><div>female</div></div></div></body></html>")
summary = ps.card_summary(empty_cards)
assert "GENDER(1 rows, EMPTY)" in summary, summary
assert ps.card_summary(html.fromstring("<html><body></body></html>")) == "no vote cards"
print("card_summary describes empty and missing cards OK")

# which vote cards are missing or empty
assert ps.vote_card_problems(empty_cards) == ["longevity card missing", "sillage card missing", "gender card has no numbers"]

# perfumers named on the page
named = html.fromstring(
    "<html><body><a href='/noses/'>Perfumers</a><h3>Perfumers</h3>"
    "<a href='/noses/David_Apel.html'><img src='x'><span>David Apel</span></a>"
    "<a href='/noses/Pierre_Negrin.html'>Pierre Negrin</a>"
    "<a href='/noses/David_Apel.html'>David Apel</a></body></html>")
assert ps.page_perfumers(named) == [
    {"name": "David Apel", "slug": "David_Apel"}, {"name": "Pierre Negrin", "slug": "Pierre_Negrin"}]
assert ps.page_perfumers(html.fromstring("<html><body><a href='/noses/'>Perfumers</a></body></html>")) == []
print("perfumers read from the page OK")

# a brand whose link text is split over several nodes, and holds punctuation and another script
split_brand = html.fromstring(
    "<html><body><h1>Aqua Lumiere Новая Заря (Novaya Zarya – The New Dawn) <small>for men</small></h1>"
    "<a href='/designers/'>Designers</a>"
    "<a href='https://www.fragrantica.com/designers/Novaa-Zara.html'><span>Новая Заря (Novaya Zarya</span> – The New Dawn)</a>"
    "<a href='/designers/Dior.html'>Dior</a></body></html>")
assert ps.heading_brand(split_brand) == "Новая Заря (Novaya Zarya – The New Dawn)", ps.heading_brand(split_brand)
# a popular designer named elsewhere on the page must not be mistaken for the brand
assert ps.heading_brand(html.fromstring(
    "<html><body><h1>Miss Dior Blooming Chanel <small>for women</small></h1>"
    "<a href='/designers/Dior.html'>Dior</a><a href='/designers/Chanel.html'>Chanel</a></body></html>")) == "Chanel"
print("brand read whole from the heading OK")

# ---- a chart nobody voted on is kept as zeros, not dropped ----
import clean  # noqa: E402
empty = clean._chart({"winter": "0", "spring": "0", "summer": "0", "fall": "0", "day": "0", "night": "0"},
                     {k: k for k in clean.WHEN_KEYS}, clean.WHEN_KEYS, relative_to_max=True)
assert empty and set(empty["share"].values()) == {0.0} and set(empty["votes"].values()) == {0}, empty
assert clean._chart(None, {k: k for k in clean.WHEN_KEYS}, clean.WHEN_KEYS) is None, "missing chart stays None"
print("charts with no votes keep zeros OK")

# ---- ad blocking: only the site, its images and Cloudflare may be reached ----
for url in ("https://www.fragrantica.com/perfume/Dior/Sauvage-31861.html", "https://fragrantica.com/x",
            "https://c.fimgs.net/mdimg/perfume/o.1.jpg", "https://challenges.cloudflare.com/turnstile/v0/api.js",
            "https://cdnjs.cloudflare.com/ajax/libs/x.js", "data:image/png;base64,AAAA", "blob:https://www.fragrantica.com/1"):
    assert bc.allowed_url(url), url
for url in ("https://securepubads.g.doubleclick.net/tag.js", "https://ads.pubmatic.com/x", "https://a.pub.network/core.js",
            "https://www.googletagmanager.com/gtm.js", "https://fragrantica.com.evil.com/x", "https://notfimgs.net/x",
            "https://example.com/?u=fragrantica.com"):
    assert not bc.allowed_url(url), url


class FakeRoute:
    def __init__(self, url):
        self.request, self.done = types.SimpleNamespace(url=url), None

    def continue_(self):
        self.done = "continue"

    def abort(self, reason):
        self.done = "abort " + reason


client = bc.BrowserClient(block_ads=True)
routes = [FakeRoute(u) for u in ("https://www.fragrantica.com/a", "https://ads.pubmatic.com/x", "https://ads.pubmatic.com/y")]
for r in routes:
    client._on_request(r)
assert [r.done for r in routes] == ["continue", "abort blockedbyclient", "abort blockedbyclient"]
assert client.requests_blocked == {"ads.pubmatic.com": 2} and "2 blocked" in client.block_summary()
font = FakeRoute("https://www.fragrantica.com/font.woff2")
font.request.resource_type = "font"
client._on_request(font)
assert font.done == "abort blockedbyclient" and client.requests_skipped == {"font": 1}
assert "skipped on allowed hosts: font x1" in client.block_summary()
page = FakeRoute("https://www.fragrantica.com/p.html")
page.request.resource_type = "document"
client._on_request(page)
assert page.done == "continue"
assert bc.BrowserClient(block_ads=False).block_summary() == "" and bc.BrowserClient().block_ads is True
print("ad blocking allowlist OK")

# a count that cannot be read makes the chart missing, not a chart of zeros
assert clean._chart({"winter": "12", "spring": "oops", "summer": "1", "fall": "2", "day": "3", "night": "4"},
                    {k: k for k in clean.WHEN_KEYS}, clean.WHEN_KEYS) is None
from archiver import charts  # noqa: E402
assert ps.GENDER_KEYS is charts.GENDER_KEYS is clean.GENDER_KEYS and clean.CHART_ORDER is charts.CHART_KEYS
print("unreadable counts drop the chart; one copy of the chart levels OK")
