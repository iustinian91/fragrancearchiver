"""Scrape perfume details using Patchright and save them to SQLite."""

import argparse
import json
import re
import unicodedata
from pathlib import Path

from lxml import html

from archiver import browser_client, paths
from archiver.charts import GENDER_KEYS, LONGEVITY_KEYS, SILLAGE_KEYS, WHEN_KEYS
from archiver.browser_client import FETCH_ERRORS, BrowserClient, PageGoneError
from archiver.db_helper import PerfumesDBHelper
from archiver.urls import read_manifest

IMAGE_DIR = paths.image_dir("perfumes")
MAX_SKIP_ATTEMPTS = 3      # a page that is skipped this many times is left alone until --retry-skipped
MAX_SKIPS_IN_A_ROW = 5     # this many skipped pages in a row stop the crawl: something has changed
INCOMPLETE_FILE = paths.log_file("perfumes_incomplete.txt")   # url and reason, one per line
INCOMPLETE_DIR = paths.log_file("perfumes_incomplete")   # the pages themselves, as they were read
GONE_FILE = paths.log_file("perfumes_gone.txt")               # pages the site no longer has (HTTP 404/410): url and reason
FAILED_FILE = paths.log_file("perfumes_failed.txt")           # pages that could not be read (blank, malformed): url and reason
PERFUME_URL = re.compile(
    r"/perfume/(?P<brand>[^/]+)/(?P<title>.+)-\d+\.html?$",
    re.IGNORECASE,
)
YEAR = r"(1[5-9]\d\d|20\d\d)"
EDGE_PUNCTUATION = " ,;:|•–—-\"“”"


def _labels(texts):
    """Whitespace-normalised, non-empty, de-duplicated strings in their original order."""
    seen, ordered = set(), []
    for text in texts:
        text = " ".join(str(text).split())
        if text and text not in seen:
            seen.add(text)
            ordered.append(text)
    return ordered


def page_image_url(tree):
    """The bottle photo (.../mdimg/perfume/o.<id>.jpg).

    Pages carry several og:image tags and the first is a generated social card, so pick
    the photo explicitly; this is also the file name the original dataset stored."""
    urls = tree.xpath("//meta[@property='og:image']/@content")
    for url in urls:
        if "/mdimg/perfume/o." in url:
            return url
    return (tree.xpath("//img[@itemprop='image']/@src") or urls or [None])[0]


def page_main_accords(tree):
    """Main accord names, strongest first, or None."""
    accords = _labels(tree.xpath(
        "//h6[normalize-space()='main accords']/following-sibling::div[1]"
        "//span[contains(@class, 'truncate')]/text()"
    ))
    return accords or None


PYRAMID_BOX = "//*[contains(concat(' ', normalize-space(@class), ' '), ' pyramid-level-container ')]"
PYRAMID_LEVELS = ("top", "middle", "base")


def _pyramid_level(box):
    """Which level a note container belongs to: top, middle, base, or another label
    (single-ingredient fragrances use 'ingredients'; unknown becomes 'general').

    In the page as served, a wrapper tag carries the level. Once the page's own script has
    run, the wrapper is gone, so fall back to the 'Top Notes' style heading just above."""
    level = box.xpath("ancestor::pyramid-level-new[1]/@notes")
    if level:
        return level[0]
    heading = " ".join(" ".join(box.xpath("preceding::h4[1]//text()")).lower().split())
    for name in PYRAMID_LEVELS:
        if heading.startswith(name + " note"):
            return name
    return "general"


def page_notes(tree):
    """Notes as {"top": [...], "middle": [...], "base": [...]}, {"general": [...]} for pages
    without a pyramid, or None."""
    levels = {}
    for box in tree.xpath(PYRAMID_BOX):
        names = levels.setdefault(_pyramid_level(box), [])
        for label in _labels(box.xpath(".//span[contains(@class, 'pyramid-note-label')]/text()")):
            if label not in names:  # the page can render the pyramid more than once
                names.append(label)
    levels = {key: names for key, names in levels.items() if names}
    if levels:
        if set(levels) <= set(PYRAMID_LEVELS):
            return {key: levels.get(key) or None for key in PYRAMID_LEVELS}
        return {"general": [name for names in levels.values() for name in names]}
    return None


def _skeleton_pair(text):
    """Lowercase letters and digits of `text`, in two forms: accents folded (é -> e) and
    accented letters dropped (é -> nothing). URL slugs treat accents either way."""
    folded = unicodedata.normalize("NFKD", text).lower()
    plain = unicodedata.normalize("NFC", text).lower()
    return (
        "".join(ch for ch in folded if ch.isascii() and ch.isalnum()),
        "".join(ch for ch in plain if ch.isascii() and ch.isalnum()),
    )


def find_name(texts, slug_text):
    """Recover a properly written name from the page.

    The URL slug has lost apostrophes, periods, ampersands and accents ("Victoria-s-Secret"),
    but it keeps the letters and digits in order. So look through `texts` (most reliable first)
    for a run of words that reduces to the same letters and digits as the slug and return
    those words as written on the page. Returns None when nothing on the page matches; the
    caller then keeps the slug's version.
    """
    wanted = set(_skeleton_pair(slug_text)) - {""}
    if not wanted:
        return None
    longest = max(len(skeleton) for skeleton in wanted)
    for text in texts:
        words = text.split()
        pairs = [_skeleton_pair(word) for word in words]
        for start, first in enumerate(pairs):
            if not any(first):
                continue  # a name starts on a real word, not on a lone '&' or '-'
            fold = plain = ""
            for end in range(start, len(words)):
                fold += pairs[end][0]
                plain += pairs[end][1]
                if any(pairs[end]) and {fold, plain} & wanted:
                    name = " ".join(words[start:end + 1]).strip(EDGE_PUNCTUATION)
                    return unicodedata.normalize("NFC", name)  # one form for accents, whatever the page used
                if len(fold) > longest and len(plain) > longest:
                    break
    return None


def page_name_candidates(tree):
    """Texts on the page that spell out the brand and the perfume name, most specific first."""
    texts = []
    for xpath in (
        "//a[contains(@href, '/designers/')]//text()",  # brand link, one string per node
        "//*[@itemprop='name']//text()",
    ):
        texts += tree.xpath(xpath)
    texts.append(" ".join(tree.xpath("(//h1)[1]/text()")))   # the name without its <small> suffix
    texts.append(" ".join(tree.xpath("(//h1)[1]//text()")))
    texts += tree.xpath("//meta[@property='og:title']/@content")
    texts += tree.xpath("//title/text()")
    return _labels(texts)


def launch_year(page_title, tree):
    """The year the page title ends with, else the 'launched in YYYY' sentence; None if neither."""
    match = re.search(r"(?<!\d)" + YEAR + r"\s*$", page_title)
    if match:
        return match.group(1)
    body = " ".join(tree.xpath("//body//text()[not(ancestor::script) and not(ancestor::style)]"))
    match = re.search(r"\blaunched in " + YEAR + r"\b", body)
    return match.group(1) if match else None


# The page as served already contains empty .tw-perf-card boxes; the vote rows (.space-y-2)
# only appear once the page's script has drawn them, so wait for those.
VOTES_BOX = ".tw-perf-card"
VOTE_CARDS = 3          # longevity, sillage and gender (price value, if shown, comes after)
VOTES_READY = VOTES_BOX + " .space-y-2"
VOTES_STOP_AT = "Reviews"  # a heading such as "All Reviews By Date"; the cards are above it


def parse_count(text):
    """'216' -> 216, '2.6k' -> 2600, '1.2M' -> 1200000. The site abbreviates large counts,
    so those are approximate (to the nearest hundred or so); None if unreadable."""
    match = re.fullmatch(r"([\d.,]+)\s*([kKmM]?)", " ".join(str(text).split()))
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    factor = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2).lower()]
    return round(number * factor)


def _chart_rows(container):
    """(label, count) pairs of a vote chart, in page order.

    Each row is a label span followed by a count span ('very weak' '216'); anything else in
    the container (the heading, 'no vote', slider marks) is skipped."""
    texts = [" ".join(t.split()) for t in container.xpath(".//span/text()")]
    texts = [t for t in texts if t]
    rows, i = [], 0
    while i < len(texts) - 1:
        label, count = texts[i], parse_count(texts[i + 1])
        if parse_count(label) is None and label.lower() != "no vote" and count is not None:
            rows.append((label.lower(), count))
            i += 2
        else:
            i += 1
    return rows


PERF_CARD = "//*[contains(concat(' ', normalize-space(@class), ' '), ' tw-perf-card ')]"
WHEN_CHART = (
    "//span[contains(@class, 'tw-rating-card-label')][normalize-space()='When To Wear']"
    "/ancestor::div[.//div[@index]][1]"
)


def _as_votes(rows, keys, by_label):
    """Vote counts as {key: "count"}, or None unless the chart has every expected level. A chart
    nobody has voted on is kept, with every count "0". `by_label` charts must carry exactly `keys`
    as their labels; the others (longevity, sillage) have been renamed by the site, so they are
    matched by position."""
    if len(rows) != len(keys) or (by_label and tuple(label for label, _ in rows) != keys):
        return None
    return {key: str(count) for key, (_, count) in zip(keys, rows)}


def page_charts(tree):
    """Vote charts on a perfume page as a dict with the keys longevity, sillage, gender and
    when_to_wear; each is {level: "votes"} (all "0" when nobody voted)
    or None (chart missing or unexpected shape).

    The site's levels for longevity (very weak..eternal) and sillage (intimate..enormous) are
    stored by position under the original names (poor..very long lasting, soft..enormous), so
    existing data and clean.py keep working. Counts above 1,000 are abbreviated by the site
    ('2.6k') and so approximate."""
    charts = dict.fromkeys(("longevity", "sillage", "gender", "when_to_wear"))
    for card in tree.xpath(PERF_CARD):
        heading = " ".join(card.xpath(".//span[contains(@class, 'uppercase')]/text()")).strip().lower()
        rows = _chart_rows(card)
        if heading == "longevity":
            charts["longevity"] = _as_votes(rows, LONGEVITY_KEYS, by_label=False)
        elif heading == "sillage":
            charts["sillage"] = _as_votes(rows, SILLAGE_KEYS, by_label=False)
        elif heading == "gender":
            charts["gender"] = _as_votes(rows, GENDER_KEYS, by_label=True)
    for chart in tree.xpath(WHEN_CHART)[:1]:
        charts["when_to_wear"] = _as_votes(_chart_rows(chart), WHEN_KEYS, by_label=True)
    return charts


def heading_brand(tree):
    """The brand as the page's heading spells it, for pages whose URL carries an older brand
    name ("A Lab on Fire" in the URL, "What We Do Is Secret" on the page): the designer link
    whose whole text ends the h1 ("<perfume> <brand> for women"). The link's text may be split
    over several nodes ("Новая Заря (Novaya Zarya – The New Dawn)"), so it is read as a whole.
    None if there is none."""
    heading = " ".join(" ".join(tree.xpath("(//h1)[1]//text()")).split())
    heading = re.sub(r"\s+for (?:women and men|women|men)$", "", heading)
    best = None
    for link in tree.xpath(
        "//a[contains(@href, '/designers/')][substring(@href, string-length(@href) - 4) = '.html']"
    ):
        text = " ".join(link.text_content().split())
        if text and heading.endswith(text) and (best is None or len(text) > len(best)):
            best = text
    return best


def check_page(tree, url):
    """Raise ValueError unless this looks like a perfume page (a title and a heading).

    A blocked, error or half-loaded page has neither; saving it would mark the URL complete
    with an empty record that no later run revisits."""
    has_title = " ".join(tree.xpath("//title/text()")).strip()
    has_heading = " ".join(tree.xpath("(//h1)[1]//text()")).strip()
    if not (has_title and has_heading):
        raise ValueError("{} does not look like a perfume page (no title or heading)".format(url))


class IncompletePageError(RuntimeError):
    """The page was read, but not everything we need had appeared on it. Carries the page."""

    def __init__(self, message, tree):
        super().__init__(message)
        self.tree = tree


DEBUG_DIR = None   # set (e.g. by the dry test) to keep the page exactly as the scraper read it


def card_summary(tree):
    """One line on the vote cards present when the page was read: label, rows, and whether the
    rows hold numbers. Shows whether a missing chart was never drawn or never parsed."""
    parts = []
    for card in tree.xpath(PERF_CARD):
        label = " ".join((card.xpath(".//span[contains(@class,'uppercase')]/text()") or ["?"])[0].split())
        rows = card.xpath(".//*[contains(@class,'space-y-2')]")
        numbers = bool(rows) and any(ch.isdigit() for ch in rows[0].text_content())
        parts.append("{}({} rows{})".format(label, len(rows[0].xpath("./*")) if rows else 0,
                                           ", numbers" if numbers else ", EMPTY"))
    return "; ".join(parts) or "no vote cards"


def page_perfumers(tree):
    """The perfumers (noses) credited on the page: [{"name": "David Apel", "slug": "David_Apel"}].
    Empty when the page names none. Their links are in the "Perfumers" section."""
    people, seen = [], set()
    for link in tree.xpath("//a[starts-with(@href, '/noses/')][substring(@href, string-length(@href) - 4) = '.html']"):
        name = " ".join(link.text_content().split())
        slug = link.get("href").rsplit("/", 1)[-1][:-len(".html")]
        if name and slug not in seen:
            seen.add(slug)
            people.append({"name": name, "slug": slug})
    return people


def vote_card_problems(tree):
    """What is missing from the longevity, sillage and gender cards: "gender card missing",
    "sillage card has no numbers". Empty if all three are there and filled."""
    cards = {}
    for card in tree.xpath(PERF_CARD):
        label = " ".join(card.xpath(".//span[contains(@class,'uppercase')]/text()")).lower()
        rows = card.xpath(".//*[contains(@class,'space-y-2')]")
        for name in ("longevity", "sillage", "gender"):
            if name in label:
                cards[name] = bool(rows) and any(ch.isdigit() for ch in rows[0].text_content())
    return ["{} card missing".format(name) if name not in cards else "{} card has no numbers".format(name)
            for name in ("longevity", "sillage", "gender") if not cards.get(name)]


def scrape_perfume(client, url):
    """Fetch and parse one perfume page. Returns (perfume dict, record tuple)."""
    match = PERFUME_URL.search(url)
    if not match:
        raise ValueError("Unrecognised perfume URL: {}".format(url))

    url_brand = match.group("brand").replace("-", " ")
    url_title = match.group("title").replace("-", " ")
    tree = client.fetch_tree(url, wait_for=VOTES_READY, stop_at=VOTES_STOP_AT,
                             lazy_boxes=VOTES_BOX, expect_boxes=VOTE_CARDS)
    check_page(tree, url)
    problems = vote_card_problems(tree)
    if problems:
        # Every perfume page has longevity, sillage and gender cards with numbers in them, even
        # with no votes, so a page without was read before it finished drawing.
        raise IncompletePageError("{}; cards when read: {}".format(", ".join(problems), card_summary(tree)), tree)

    names = page_name_candidates(tree)
    # The page's own designer link is the authority on the brand: the URL spelling can be an older
    # name or a transliteration ("Novaya-Zarya" for "Новая Заря (Novaya Zarya – The New Dawn)").
    page_brand = heading_brand(tree) or find_name(names, url_brand)
    page_title_name = find_name(names, url_title)
    brand = page_brand or url_brand

    perfume_image_url = page_image_url(tree)
    perfume_image_file_name = None
    if perfume_image_url:
        file_name = perfume_image_url.split("/")[-1]
        # Only name a file that is on disk; the address is kept so the picture can be fetched later.
        if client.download_image(perfume_image_url, IMAGE_DIR / file_name):
            perfume_image_file_name = file_name

    page_titles = tree.xpath("//title/text()")
    page_title = " ".join(page_titles[0].split()) if page_titles else ""
    perfume = {
        "title": page_title_name or url_title,
        "image": perfume_image_file_name,
        "image_url": perfume_image_url,
        # Where each name came from, so clean.py only re-cases the slug-derived ones.
        "title_source": "page" if page_title_name else "url",
        "brand_source": "page" if page_brand else "url",
        # Raw page title, kept so names and years can be re-derived without re-crawling.
        "page_title": page_title,
    }

    main_accords = page_main_accords(tree)
    notes = page_notes(tree)

    charts = page_charts(tree)
    missing = [name for name in ("longevity", "sillage", "gender") if charts[name] is None]
    if missing:
        print("  [votes] {}: no {} stored; cards when read: {}".format(
            url.rsplit("/", 1)[-1], "/".join(missing), card_summary(tree)), flush=True)
        if DEBUG_DIR:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            (DEBUG_DIR / url.rsplit("/", 1)[-1]).write_bytes(html.tostring(tree))

    record = (
        brand, json.dumps(perfume), launch_year(page_title, tree), json.dumps(main_accords),
        json.dumps(notes), json.dumps(charts["longevity"]), json.dumps(charts["sillage"]),
        json.dumps(charts["gender"]), json.dumps(charts["when_to_wear"]),
        json.dumps(page_perfumers(tree)),
    )
    return perfume, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=str(paths.database_file("perfumes.sqlite")), help="SQLite file to write (default: %(default)s)"
    )
    parser.add_argument(
        "--limit", type=int,
        help="scrape at most this many pages; try it with a scratch --db before a full crawl",
    )
    parser.add_argument(
        "--retry-skipped", action="store_true",
        help="try again the pages given up on after %d failed tries" % MAX_SKIP_ATTEMPTS,
    )
    browser_client.add_browser_arguments(parser)
    args = parser.parse_args(argv)
    browser_client.apply_browser_arguments(args)

    db = PerfumesDBHelper(args.db)
    db.setup()
    if args.retry_skipped:
        db.reset_skips()
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        manifest = paths.manifest_file("perfumes_manifest.txt")
        perfume_urls = read_manifest(manifest)
        if not perfume_urls:
            raise FileNotFoundError("{} is missing or empty; run perfumes_extract_manifest.py".format(manifest))
        completed = db.completed_urls()
    except (OSError, ValueError, RuntimeError) as exc:
        print("Could not prepare perfume scrape: {}".format(exc))
        db.close()
        return 1

    total = len(perfume_urls)
    gone = db.gone_urls()
    given_up = db.given_up_urls(MAX_SKIP_ATTEMPTS)
    pending = [url for url in perfume_urls if url not in completed and url not in gone and url not in given_up]
    if args.limit is not None:
        pending = pending[:args.limit]
    print(
        "Perfume scrape: {} URLs in manifest, {} already complete, {} to scrape now.".format(
            total, len(completed), len(pending)
        ),
        flush=True,
    )
    if given_up:
        print("{} page(s) are skipped: they failed {} times (see {}); use --retry-skipped to try them again.".format(
            len(given_up), MAX_SKIP_ATTEMPTS, INCOMPLETE_FILE), flush=True)

    from_page = {"title": 0, "brand": 0}
    # How many scraped rows actually have each field, so an extraction that stops working
    # (e.g. after a site redesign) shows up after a short test run instead of a long crawl.
    filled = dict.fromkeys(
        ("year", "accords", "notes", "longevity", "sillage", "gender", "when_to_wear", "perfumers"), 0
    )
    scraped = 0
    incomplete, newly_gone, unreadable = [], [], []
    skipped_in_a_row = 0

    try:
        with BrowserClient() as client:
            for position, url in enumerate(pending, start=1):
                try:
                    perfume, record = scrape_perfume(client, url)
                except IncompletePageError as exc:
                    # Not marked complete, so the next run tries it again; the crawl goes on.
                    # The page is kept so the reason can be looked into later.
                    print("  [incomplete] {}: {}".format(url.rsplit("/", 1)[-1], exc), flush=True)
                    incomplete.append(url)
                    INCOMPLETE_DIR.mkdir(parents=True, exist_ok=True)
                    (INCOMPLETE_DIR / url.rsplit("/", 1)[-1]).write_bytes(html.tostring(exc.tree))
                    if db.note_skip(url, str(exc)) == 1:    # one log line per page, not one per try
                        with open(paths.ensure_parent(INCOMPLETE_FILE), "a", encoding="utf-8") as log:
                            log.write("{}\t{}\n".format(url, exc))
                    skipped_in_a_row += 1
                    if skipped_in_a_row >= MAX_SKIPS_IN_A_ROW:
                        raise RuntimeError("{} pages in a row were incomplete (last: {}); the page layout may have changed".format(
                            skipped_in_a_row, exc))
                    continue
                except PageGoneError as exc:
                    print("  [gone] {}: {}".format(url.rsplit("/", 1)[-1], exc), flush=True)
                    db.mark_gone(url, str(exc))
                    newly_gone.append(url)
                    with open(paths.ensure_parent(GONE_FILE), "a", encoding="utf-8") as log:
                        log.write("{}\t{}\n".format(url, exc))
                    continue
                except ValueError as exc:
                    # Malformed URL or a blank page: skip it, but a run of them means something
                    # is wrong with the site or the browser, so stop then.
                    print("  [unreadable] {}: {}".format(url.rsplit("/", 1)[-1], exc), flush=True)
                    unreadable.append(url)
                    if db.note_skip(url, str(exc)) == 1:
                        with open(paths.ensure_parent(FAILED_FILE), "a", encoding="utf-8") as log:
                            log.write("{}\t{}\n".format(url, exc))
                    skipped_in_a_row += 1
                    if skipped_in_a_row >= MAX_SKIPS_IN_A_ROW:
                        raise RuntimeError("{} pages in a row could not be read (last: {})".format(
                            skipped_in_a_row, exc))
                    continue
                skipped_in_a_row = 0
                db.add_record_for_url(url, record)
                completed.add(url)
                scraped += 1
                from_page["title"] += perfume["title_source"] == "page"
                from_page["brand"] += perfume["brand_source"] == "page"
                filled["year"] += record[2] is not None
                for field, value in zip(
                        ("accords", "notes", "longevity", "sillage", "gender", "when_to_wear", "perfumers"),
                        record[3:],
                ):
                    filled[field] += value not in ("null", "[]")
                print({
                    "progress": "{}/{}".format(position, len(pending)),
                    "completed_total": len(completed),
                    "brand": record[0],
                    "title": perfume["title"],
                    "year": record[2],
                    "names_from": "{}/{}".format(perfume["title_source"], perfume["brand_source"]),
                }, flush=True)
    except (*FETCH_ERRORS, ValueError) as exc:
        print(
            "Scrape paused: {}. {} URLs are recorded as complete; rerun to continue "
            "with the remaining URLs.".format(exc, len(completed)),
            flush=True,
        )
        return 1
    finally:
        db.close()

    if scraped:
        print(
            "Done: {} page(s). Names written as on the page: {} titles, {} brands "
            "(the rest fall back to the URL slug).".format(
                scraped, from_page["title"], from_page["brand"]
            )
        )
        print("Rows with: " + ", ".join(
            "{} {}/{}".format(field, count, scraped) for field, count in filled.items()
        ))
    if incomplete:
        print("{} page(s) were not recorded because their vote cards had not all appeared "
              "(listed in {}, pages kept in {}/); run the scraper again to retry them.".format(
                  len(incomplete), INCOMPLETE_FILE, INCOMPLETE_DIR))
    if newly_gone:
        print("{} page(s) no longer exist on the site (listed in {}); they will not be asked for again.".format(
            len(newly_gone), GONE_FILE))
    if unreadable:
        print("{} page(s) could not be read (listed in {}); they are tried again on the next run.".format(
            len(unreadable), FAILED_FILE))
    print("Completed: {} perfume URLs processed.".format(len(completed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
