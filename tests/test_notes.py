"""Run with: python tests/test_notes.py  (needs lxml; no browser)."""
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
# a page without note photos (the old layout's plain thumbnails do not count) gives none
old = html.fromstring("<html><body><h1>Amber</h1><h3>Photos</h3><div><div><img src='f.9.jpg'></div></div></body></html>")
assert ns.note_photo_urls(old, "https://www.fragrantica.com/notes/Amber-9.html") == []
print("note page: title and the 3 photos OK; perfume thumbnails ignored")


# ---- the scraper stores each note's category from notes_categories.json ----
import json  # noqa: E402
import os  # noqa: E402
import sqlite3  # noqa: E402
import tempfile  # noqa: E402

BERGAMOT = "https://www.fragrantica.com/notes/Bergamot-75.html"
with tempfile.TemporaryDirectory() as tmp:
    os.chdir(tmp)
    ns.IMAGE_DIR = Path("images")
    Path("notes_manifest.txt").write_text(BERGAMOT + "\n")
    ns.CATEGORIES_FILE = Path("notes_categories.json")

    class Live(Client):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    ns.BrowserClient = Live
    ns.GONE_FILE, ns.FAILED_FILE = Path("g.txt"), Path("f.txt")

    Path("notes_categories.json").write_text(json.dumps({BERGAMOT: "CITRUS SMELLS"}))
    assert ns.main(["--db", "n.sqlite"]) == 0
    conn = sqlite3.connect("n.sqlite")
    assert conn.execute("SELECT note, category, url, page_id FROM notes").fetchall() == [
        ("Bergamot", "CITRUS SMELLS", BERGAMOT, "75")]
    conn.close()

    # without a categories file the note is still stored, with no category
    Path("notes_categories.json").unlink()
    assert ns.main(["--db", "m.sqlite"]) == 0
    conn = sqlite3.connect("m.sqlite")
    assert conn.execute("SELECT note, category FROM notes").fetchall() == [("Bergamot", None)]
    conn.close()
print("note category stored with the note OK")
