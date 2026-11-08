"""Run with: python tests/test_notes_manifest.py  (needs lxml; no browser)."""
import sys
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

import notes_extract_manifest as nem  # noqa: E402
from browser_client import BASE_URL, matching_urls  # noqa: E402

# A trimmed copy of the notes index as the redesigned site serves it: the menu's plain /notes/
# link, 40 note cards (one repeated), a link to another site and one to a designer.
tree = html.fromstring((ROOT / "tests/fixtures/notes_index.html").read_text(encoding="utf-8"))
urls = matching_urls(tree, BASE_URL + "/notes/", nem.NOTE_PATH)
assert len(urls) == 40 and len(set(urls)) == 40, len(urls)
assert urls[0] == "https://www.fragrantica.com/notes/Bergamot-75.html"
assert all(u.startswith(BASE_URL + "/notes/") and u.endswith(".html") for u in urls)
assert not any("evil" in u or "designers" in u or u.endswith("/notes/") for u in urls)
# the old selector finds nothing on this page, which is why it had to change
assert tree.xpath("//div[@class='notebox']/a/@href") == []
print("notes index links OK")
