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


# ---- canonical URLs, and the guard against replacing a manifest with a much smaller list ----
import os  # noqa: E402
import tempfile  # noqa: E402

import urls as u  # noqa: E402

assert u.canonical_url("http://www.fragrantica.com/notes/Bergamot-75.html?x=1#y") == "https://www.fragrantica.com/notes/Bergamot-75.html"
assert u.canonical_url("https://fragrantica.com/perfume/A/B-1.html") == "https://www.fragrantica.com/perfume/A/B-1.html"
assert u.canonical_url("https://example.com/a") == "https://example.com/a"
assert u.too_small_to_replace(5, 10) and not u.too_small_to_replace(9, 10) and not u.too_small_to_replace(5, 0)
assert not u.too_small_to_replace(5, 10, force=True)

with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    old = "".join("{}/notes/N{}-{}.html\n".format(BASE_URL, i, i) for i in range(10))
    Path("notes_manifest.txt").write_text(old)

    class OneNote:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def fetch_tree(s, url): return html.fromstring('<a href="/notes/N1-1.html">x</a>')

    nem.BrowserClient = OneNote
    assert nem.main([]) == 1 and Path("notes_manifest.txt").read_text() == old
    assert Path("notes_manifest.new.txt").read_text().split() == [BASE_URL + "/notes/N1-1.html"]
    assert nem.main(["--force"]) == 0 and len(Path("notes_manifest.txt").read_text().split()) == 1
print("notes manifest guard OK")
