"""Move the data files of an older checkout into the data/ folder layout.

    python migrate_layout.py            # show what would move
    python migrate_layout.py --apply    # move it

Nothing is overwritten: if the destination already exists, that item is left where it is and
reported. Run it from the folder the old files are in."""

import argparse
import shutil
from pathlib import Path

from archiver import paths

# (old name in the project folder, folder under data/, new name)
FILES = [
    ("perfumes_manifest.txt", "manifests", None),
    ("perfumes_manifest.txt.tmp", "manifests", None),
    ("perfumes_manifest.state.json", "manifests", None),
    ("perfumes_manifest.state.json.tmp", "manifests", None),
    ("perfumes_manifest.empty.txt", "manifests", None),
    ("perfumes_manifest.short.txt", "manifests", None),
    ("perfumes_manifest.new.txt", "manifests", None),
    ("notes_manifest.txt", "manifests", None),
    ("notes_manifest.txt.tmp", "manifests", None),
    ("notes_manifest.new.txt", "manifests", None),
    ("perfumes.sqlite", "databases", None),
    ("notes.sqlite", "databases", None),
    ("app.db", "databases", None),
    ("perfumes.json", "exports", None),
    ("perfumes_clean.json", "exports", None),
    ("perfumes_incomplete.txt", "logs", None),
    ("perfumes_incomplete", "logs", None),
    ("perfumes_gone.txt", "logs", None),
    ("perfumes_failed.txt", "logs", None),
    ("notes_gone.txt", "logs", None),
    ("notes_failed.txt", "logs", None),
    ("fragrantica_images", "", "images"),
    ("browser_profile", "", None),
]
RUN_PREFIXES = ("dry_run", "smoke_run")   # test run folders, old and new names


def plan(source=Path(".")):
    """List of (from, to) moves for everything found in `source`."""
    data = paths.standard("")
    moves = []
    for old, folder, new in FILES:
        src = source / old
        if src.exists():
            moves.append((src, data / folder / (new or old)))
    for item in sorted(source.iterdir()):
        if item.is_dir() and item.name.startswith(RUN_PREFIXES):
            moves.append((item, data / "dry_runs" / item.name))
    return moves


def migrate(source=Path("."), apply=False):
    done, skipped = [], []
    for src, dst in plan(source):
        if dst.exists():
            skipped.append((src, dst))
            continue
        if apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        done.append((src, dst))
    return done, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="actually move the files (default: only list them)")
    args = ap.parse_args()
    done, skipped = migrate(apply=args.apply)
    verb = "moved" if args.apply else "would move"
    for src, dst in done:
        print(f"{verb}: {src} -> {dst}")
    for src, dst in skipped:
        print(f"left alone (destination exists): {src} -> {dst}")
    if not done and not skipped:
        print("nothing to move")
    elif not args.apply and done:
        print("\nRun again with --apply to move them.")


if __name__ == "__main__":
    main()
