"""Run with: python tests/test_notes.py  (needs lxml; no browser)."""
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

import notes_scraper as ns  # noqa: E402

tree = html.fromstring((ROOT / "tests/fixtures/note_bergamot.html").read_text(encoding="utf-8"))


class Client:
    def __init__(self):
        self.downloads = []

    def fetch_tree(self, url):
        return tree

    def download_image(self, url, dest):
        self.downloads.append((url, Path(dest).name))


client = Client()
title, names = ns.scrape_note(client, "https://www.fragrantica.com/notes/Bergamot-75.html")
assert title == "Bergamot", title
# the note's three photos, named without the cache-buster query; perfume thumbnails ignored
assert names == ["t.75.jpg", "t2.75.jpg", "t3.75.jpg"], names
assert [u for u, _ in client.downloads] == [
    "https://fimgs.net/mdimg/sastojci/t.75.jpg?1733161295",
    "https://www.fragrantica.com/mdimg/sastojci/t2.75.jpg?1733161288",   # a relative link on the page
    "https://www.fragrantica.com/mdimg/sastojci/t3.75.jpg?1733161293"], client.downloads
# the layout used until 2022 still works
old = html.fromstring("<html><body><h1>Amber</h1><h3>Photos</h3><div><div><img src='f.9.jpg'></div></div></body></html>")
assert ns.note_photo_urls(old, "https://www.fragrantica.com/notes/Amber-9.html") == [
    "https://www.fragrantica.com/notes/f.9.jpg"]
print("note page: title and the 3 photos OK; perfume thumbnails ignored; old layout still read")
