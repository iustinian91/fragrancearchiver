"""Run with: python tests/test_votes.py  (needs lxml; patchright is not used)."""
import sys
import types
from pathlib import Path

from lxml import html

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
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
assert page_votes(zero) == (None, None), "no votes at all is stored as missing"

# ---- scrolling for the vote cards stops at the Reviews heading ----
import browser_client as bc  # noqa: E402


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
