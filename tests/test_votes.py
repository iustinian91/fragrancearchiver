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

tree = html.fromstring((ROOT / "tests/fixtures/performance_section.html").read_text(encoding="utf-8"))
longevity, sillage = ps.page_votes(tree)
assert longevity == {"poor": "216", "weak": "403", "moderate": "2600", "long lasting": "6000",
                     "very long lasting": "5300"}, longevity
assert sillage == {"soft": "620", "moderate": "3600", "heavy": "6600", "enormous": "4000"}, sillage
assert ps.parse_count("1,234") == 1234 and ps.parse_count("1.2M") == 1200000 and ps.parse_count("x") is None
assert ps.page_votes(html.fromstring("<html><body><p>nothing</p></body></html>")) == (None, None)
zero = html.fromstring(
    ps_html := (ROOT / "tests/fixtures/performance_section.html").read_text(encoding="utf-8")
)
for span in zero.xpath("//span[contains(@class,'text-[10px]')]"):
    span.text = "0"
assert ps.page_votes(zero) == (None, None), "no votes at all is stored as missing"
print("vote extraction OK")
