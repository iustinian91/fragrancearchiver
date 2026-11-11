"""migrate_layout.py moves old root-level data into data/, and never overwrites."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

work = Path(tempfile.mkdtemp())
os.chdir(work)
os.environ.pop("ARCHIVER_DATA_DIR", None)

import migrate_layout  # noqa: E402

(work / "perfumes.sqlite").write_text("db")
(work / "perfumes_manifest.txt").write_text("m")
(work / "fragrantica_images" / "perfumes").mkdir(parents=True)
(work / "fragrantica_images" / "perfumes" / "a.jpg").write_text("img")
(work / "perfumes_incomplete").mkdir()
(work / "smoke_run_1").mkdir()
(work / "notes.sqlite").write_text("new-notes")
(work / "data" / "databases").mkdir(parents=True)
(work / "data" / "databases" / "notes.sqlite").write_text("already here")

# listing only: nothing moves
done, skipped = migrate_layout.migrate(Path("."), apply=False)
assert (work / "perfumes.sqlite").exists() and len(done) == 5 and len(skipped) == 1, (done, skipped)

done, skipped = migrate_layout.migrate(Path("."), apply=True)
d = work / "data"
assert (d / "databases" / "perfumes.sqlite").read_text() == "db"
assert (d / "manifests" / "perfumes_manifest.txt").exists()
assert (d / "images" / "perfumes" / "a.jpg").exists()
assert (d / "logs" / "perfumes_incomplete").is_dir()
assert (d / "dry_runs" / "smoke_run_1").is_dir()
assert not (work / "perfumes.sqlite").exists() and not (work / "fragrantica_images").exists()
# the existing destination was not overwritten, and the source stayed
assert (d / "databases" / "notes.sqlite").read_text() == "already here"
assert (work / "notes.sqlite").read_text() == "new-notes"

# second run: only the left-alone item is reported
done, skipped = migrate_layout.migrate(Path("."), apply=True)
assert done == [] and len(skipped) == 1
print("ok")
