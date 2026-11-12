"""Where the site lives, and how its page URLs are written down.

Manifests and progress ledgers key on the URL, so one page must always have one spelling:
https, with www, and no query string or fragment."""

import json
from urllib.parse import urljoin, urlsplit

BASE_URL = "https://www.fragrantica.com"
SITE_HOSTS = {urlsplit(BASE_URL).netloc, urlsplit(BASE_URL).netloc.removeprefix("www.")}
MIN_KEEP_SHARE = 0.9   # a new manifest with fewer than 90% of the old one's URLs does not replace it


def canonical_url(url):
    """`url` in the one canonical spelling if it is on the site; anything else is returned as is.
    The older manifests were written with http://, which would otherwise look like new pages."""
    url = str(url).strip()
    parts = urlsplit(url)
    if parts.netloc.lower() in SITE_HOSTS:
        return urljoin(BASE_URL, parts.path)
    return url


def read_manifest(path):
    """The unique canonical URLs in a manifest file, in order; [] if there is no such file."""
    try:
        with open(path, encoding="utf-8") as manifest:
            return list(dict.fromkeys(canonical_url(line) for line in manifest if line.strip()))
    except FileNotFoundError:
        return []


def too_small_to_replace(new_count, old_count, force=False):
    """True if a new manifest is so much smaller than the existing one that it is probably an
    incomplete crawl (or a trial run): such a manifest must not overwrite the good one."""
    return bool(old_count) and not force and new_count < old_count * MIN_KEEP_SHARE


def read_categories(path):
    """{note url: category} from a notes_categories.json file; {} if it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError):
        return {}
    return {canonical_url(url): name for url, name in data.items()} if isinstance(data, dict) else {}
