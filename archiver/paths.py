"""Where the commands keep the files they read and write.

Everything generated lives under one folder, `data/` (override with the ARCHIVER_DATA_DIR
environment variable), sorted by kind:

    data/manifests/        perfumes_manifest.txt, notes_manifest.txt and their working files
    data/databases/        perfumes.sqlite, notes.sqlite, app.db
    data/exports/          perfumes.json, perfumes_clean.json
    data/images/           perfumes/, notes/
    data/logs/             lists of skipped pages, and the pages themselves
    data/browser_profile/  the browser's cookies, kept between runs
    data/dry_runs/         one folder per dry test run

The dry test and the offline tests call `use_flat(folder)` first, which puts every file straight
into `folder` with its plain name instead; a dry run folder is then one flat, easy-to-zip pile.
Call it before importing the commands: they work out their paths when they are imported."""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("ARCHIVER_DATA_DIR", "data"))
FLAT = False


def use_flat(folder="."):
    """Put every file directly in `folder` (default: the current folder), under its plain name."""
    global DATA_DIR, FLAT
    DATA_DIR, FLAT = Path(folder), True


def _place(folder, name):
    return DATA_DIR / name if FLAT else DATA_DIR / folder / name


def manifest_file(name):
    return _place("manifests", name)


def database_file(name):
    return _place("databases", name)


def export_file(name):
    return _place("exports", name)


def log_file(name):
    """A list of skipped pages, or the folder holding the skipped pages themselves."""
    return _place("logs", name)


def image_dir(kind):
    """Where the pictures of `kind` ("perfumes" or "notes") are kept."""
    return DATA_DIR / ("fragrantica_images" if FLAT else "images") / kind


def profile_dir():
    return DATA_DIR / "browser_profile"


def standard(folder, name=""):
    """The normal (non-flat) location of a file, whatever `use_flat` was told: the dry test uses
    it to find the real data it must leave alone."""
    return Path(os.environ.get("ARCHIVER_DATA_DIR", "data")) / folder / name


def ensure_parent(path):
    """Create the folder a file goes in; returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
