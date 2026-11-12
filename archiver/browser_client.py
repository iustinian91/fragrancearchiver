"""Shared Patchright browser helpers for the FragranceArchiver crawlers."""

import argparse
import os
import random
from collections import Counter, deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import sleep
from urllib.parse import urljoin, urlsplit

from lxml import html
from patchright.sync_api import Error as PatchrightError
from patchright.sync_api import sync_playwright

from archiver import paths
from archiver.urls import BASE_URL, SITE_HOSTS, canonical_url  # noqa: F401  (re-exported for the crawlers)

REQUEST_TIMEOUT_MS = 60000
REQUEST_DELAYS = (2.5, 10.0)
IMAGE_DELAYS = (0.5, 1.5)  # images come from the CDN right after a page; no need to wait as long
CHALLENGE_WAIT_SECONDS = 20   # how long a Cloudflare "checking your browser" page may take to clear itself
MAX_RATE_LIMIT_RETRIES = 2        # a page that is rate limited is tried 3 times in all (see MAX_CONSECUTIVE_BLOCKS)
MAX_RETRY_AFTER_SECONDS = 900     # a Retry-After longer than 15 minutes ends the run instead of sleeping
SERVER_RETRIES = 2                # HTTP 5xx: tried again this many times, after a pause
SERVER_RETRY_WAIT_SECONDS = 30
MAX_UNREADABLE_IN_A_ROW = 3       # pages in a row that could not be read (not blocked) before a crawl stops
MISSING_PAGE_STATUSES = (404, 410)
MAX_CONSECUTIVE_BLOCKS = 3    # a 429 or a challenge this many times in a row ends the run
BLOCK_WINDOW = 20             # the last 20 fetches are watched ...
MAX_BLOCKS_IN_WINDOW = 5      # ... and 5 blocks among them end the run, even with successes in between
MAX_SLOWDOWN = 8.0            # the delays never grow beyond 8 times their normal length
CLEAN_FETCHES_TO_SPEED_UP = 50   # blocks-free fetches before the delays are halved again
PROFILE_DIR = None            # browser profile folder: None = not decided yet, "" = fresh browser each run
RATE_LIMIT_COOLDOWN_SECONDS = 300
RETRY_AFTER_BUFFER_SECONDS = 2
SCROLL_STEP_WAIT_MS = 500
STABLE_SCROLL_CHECKS = 3
MAX_SCROLL_STEPS = 600   # a designer with thousands of perfumes needs a few hundred steps
MAX_LAZY_STEPS = 20
BOX_FILL_WAIT_MS = 1500      # how long a placeholder box gets to fill in once it is on screen
BOX_FILL_ATTEMPTS = 4        # how many times a box is scrolled to again before giving up on it
BOX_APPEAR_WAIT_MS = 700     # after the last known box, how long to wait (per step) for another to be created
BOX_LAST_LOOK_MS = 2500      # at the end of the page, how long a late box may still take to appear
BOX_APPEAR_STEPS = 24        # how many such steps down the page before concluding there are no more boxes
SLOW_SCROLL_STEPS = 12       # the slow second pass when a box stayed empty
SLOW_SCROLL_WAIT_MS = 600
HEADLESS = False

# Ad blocking (on by default, --no-adblock turns it off): every request to a host that is not listed here is aborted. The
# pages carry about 45 ad, tracker and cookie-sync hosts the scraper never reads. Cloudflare is
# allowed so its challenge can run, and the site's own hosts so the vote charts can load.
BLOCK_ADS = True
ALLOWED_HOSTS = (
    "fragrantica.com",           # the site, and its API for the vote charts
    "fimgs.net",                 # perfume and note images
    "cloudflare.com",            # the challenge page (challenges.cloudflare.com, cdnjs...)
    "cloudflareinsights.com",
)
# With ad blocking on, these kinds of request are dropped even from allowed hosts: the scraper
# reads text, and downloads the one picture it keeps itself. Every one is a request the site's
# servers do not have to answer. (Page images are kept: without them the page is shorter and
# the lazy vote cards are scrolled into view differently.)
SKIPPED_RESOURCE_TYPES = ("media", "font")
LOCAL_SCHEMES = ("data", "blob", "about", "chrome", "chrome-extension", "devtools")


def allowed_url(url, allowed_hosts=ALLOWED_HOSTS):
    """True if a request to `url` may go out while ad blocking is on: a host on the allowlist or
    one of its subdomains, or something that never leaves the browser (data:, blob:)."""
    parts = urlsplit(url)
    if parts.scheme in LOCAL_SCHEMES:
        return True
    host = (parts.hostname or "").lower().rstrip(".")
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)


class ScrollIncompleteError(RuntimeError):
    """Raised when a lazy-loaded list is still growing when the scroll limit is reached."""


class PageGoneError(RuntimeError):
    """The site says the page no longer exists (HTTP 404/410). Not a block: skip it and go on."""


class TooManyBlocksError(RuntimeError):
    """Raised when the site has answered MAX_CONSECUTIVE_BLOCKS requests in a row with a rate
    limit (HTTP 429) or a challenge: carrying on would only make things worse."""


class CloudflareChallengeError(RuntimeError):
    """Raised when a Cloudflare challenge/interstitial is detected."""


# Everything a crawl can raise while fetching: Patchright errors (navigation timeouts, a
# closed browser) do not derive from RuntimeError, so they must be listed explicitly.
FETCH_ERRORS = (PatchrightError, RuntimeError, OSError)


def cloudflare_challenge_reason(page):
    """Return a diagnostic reason if the current page appears to be a challenge."""
    final_url = page.url.lower()
    if "__cf_chl_rt_tk" in final_url or "/cdn-cgi/challenge-platform/" in final_url:
        return "Cloudflare challenge URL detected"

    try:
        title = page.title().strip().lower()
    except PatchrightError:
        title = ""

    if title.startswith(("just a moment", "attention required")):
        return "Cloudflare challenge/interstitial title detected: {!r}".format(title)

    try:
        body_text = page.locator("body").inner_text(timeout=3000).lower()
    except PatchrightError:
        body_text = ""

    phrases = (
        "verify you are human",
        "performing security verification",
        "checking your browser",
        "enable javascript and cookies to continue",
    )
    if any(phrase in body_text for phrase in phrases):
        return "Cloudflare verification text detected in page body"

    return None


IMAGE_SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8", b"RIFF")  # jpeg, png, gif, webp


# A popup or ad overlay may lock the page's scrolling (body {overflow: hidden}); undo that so the
# page can be scrolled to read what is on it. Nothing is clicked or removed.
UNLOCK_SCROLL_JS = """() => {
  for (const el of [document.documentElement, document.body]) {
    if (el && getComputedStyle(el).overflowY === 'hidden') el.style.setProperty('overflow', 'visible', 'important');
  }
}"""

# The site's registration prompt (#registration-nudge-overlay) covers the page and locks scrolling.
# A style rule added to every page before it loads keeps it from ever showing; nothing is clicked.
# (A hidden overlay can still leave the scroll lock behind, which UNLOCK_SCROLL_JS undoes.)
HIDE_POPUPS_CSS = "#registration-nudge-overlay { display: none !important; pointer-events: none !important; }"
HIDE_POPUPS_JS = """(() => {
  // This runs before the page has any elements, so wait for one to put the rule in.
  const add = () => {
    const root = document.head || document.documentElement;
    if (!root) return false;
    if (!document.getElementById('archiver-hide-popups')) {
      const style = document.createElement('style');
      style.id = 'archiver-hide-popups';
      style.textContent = %s;
      root.appendChild(style);
    }
    return true;
  };
  if (!add()) {
    const watcher = new MutationObserver(() => { if (add()) watcher.disconnect(); });
    watcher.observe(document, {childList: true, subtree: true});
  }
  document.addEventListener('DOMContentLoaded', add);   // again, in case the page replaced <head>
})();""" % repr(HIDE_POPUPS_CSS)


def image_problem(data):
    """Why `data` cannot be an image file, or None if it looks like one."""
    if not data:
        return "empty response"
    if not data.startswith(IMAGE_SIGNATURES):
        return "not an image (starts with {!r})".format(data[:12])
    return None


def matching_urls(tree, base_url, pattern):
    """Return unique page URLs whose path matches `pattern`.

    Relative links are resolved against `base_url` first. Only links on the site itself are
    kept, written with the canonical host and without query string or fragment, so one page
    always has exactly one manifest URL (www and non-www links would otherwise be two).
    """
    urls = []
    for href in tree.xpath("//a[@href]/@href"):
        parts = urlsplit(urljoin(base_url, href))
        if parts.netloc.lower() not in SITE_HOSTS or not pattern.fullmatch(parts.path):
            continue
        urls.append(urljoin(BASE_URL, parts.path))
    return list(dict.fromkeys(urls))


def add_browser_arguments(parser):
    """The command-line options every crawler shares: ad blocking and the browser profile."""
    parser.add_argument(
        "--no-adblock", action="store_true", dest="no_adblock",
        help="load everything the page asks for. By default requests to every host except "
             "fragrantica.com, fimgs.net and Cloudflare (ads and trackers) are aborted, and what "
             "was blocked is printed when the browser closes")
    parser.add_argument(
        "--profile", default=str(paths.profile_dir()), metavar="DIR",
        help="folder that keeps the browser's cookies between runs, so a challenge that was "
             "cleared once stays cleared (default %(default)s)")
    parser.add_argument(
        "--no-profile", action="store_true", dest="no_profile",
        help="start every run with a fresh browser instead")


def apply_browser_arguments(args):
    """Turn those options into the module settings BrowserClient reads. Settings a caller made
    first (the dry test) are kept."""
    global BLOCK_ADS, PROFILE_DIR
    if args.no_adblock:
        BLOCK_ADS = False
    if PROFILE_DIR is None:
        PROFILE_DIR = "" if args.no_profile else args.profile


class BrowserClient:
    """Own a visible Patchright Chromium browser and its page/context.

    Politeness: every pause before a request is multiplied by `slowdown`. A rate limit (429) or a
    challenge doubles it (up to MAX_SLOWDOWN); CLEAN_FETCHES_TO_SPEED_UP fetches without one halve
    it again. MAX_CONSECUTIVE_BLOCKS in a row raise TooManyBlocksError, and the crawl stops."""

    slowdown = 1.0         # class defaults, so a client made without __init__ (tests) still works
    block_streak = 0       # blocks in a row, reset by any fetch that went through
    recent = None          # the last BLOCK_WINDOW fetches: True = blocked
    clean_streak = 0       # fetches since the last block
    _persistent = False

    def __init__(self, headless=HEADLESS, timeout_ms=REQUEST_TIMEOUT_MS, block_ads=None,
                 allowed_hosts=None, profile_dir=None):
        self.profile_dir = PROFILE_DIR if profile_dir is None else profile_dir
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.block_ads = BLOCK_ADS if block_ads is None else block_ads
        self.allowed_hosts = ALLOWED_HOSTS if allowed_hosts is None else tuple(allowed_hosts)
        self.requests_allowed = Counter()    # host -> requests let through (with ad blocking on)
        self.requests_blocked = Counter()    # host -> requests aborted
        self.requests_skipped = Counter()    # resource type -> requests dropped on allowed hosts
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def __enter__(self):
        self._playwright = sync_playwright().start()
        try:
            if self.profile_dir:
                # A persistent profile keeps cookies (such as a cleared challenge) between runs.
                folder = Path(self.profile_dir).resolve()
                folder.mkdir(parents=True, exist_ok=True)
                self.context = self._playwright.chromium.launch_persistent_context(
                    str(folder), headless=self.headless)
                self._persistent = True
            else:
                self.browser = self._playwright.chromium.launch(headless=self.headless)
                self.context = self.browser.new_context()
            self.context.add_init_script(HIDE_POPUPS_JS)
            if self.block_ads:
                self.context.route("**/*", self._on_request)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(self.timeout_ms)
        except BaseException as exc:
            # __exit__ is not called when __enter__ fails; don't leak the driver process.
            self.__exit__(None, None, None)
            text = str(exc).lower()
            if self.profile_dir and any(w in text for w in ("singleton", "profile appears to be in use", "already in use")):
                raise RuntimeError(
                    "the browser profile folder '{}' is in use by another browser (a crawl still "
                    "running, or one that did not close). Close it, or use --profile OTHER_FOLDER "
                    "or --no-profile".format(self.profile_dir)) from exc
            raise
        return self

    def _on_request(self, route):
        """Let a request through if its host is allowed, otherwise abort it."""
        url = route.request.url
        host = (urlsplit(url).hostname or urlsplit(url).scheme).lower()
        kind = getattr(route.request, "resource_type", "")
        if allowed_url(url, self.allowed_hosts) and kind in SKIPPED_RESOURCE_TYPES:
            self.requests_skipped[kind] += 1
            route.abort("blockedbyclient")
        elif allowed_url(url, self.allowed_hosts):
            self.requests_allowed[host] += 1
            route.continue_()
        else:
            self.requests_blocked[host] += 1
            route.abort("blockedbyclient")

    def block_summary(self):
        """One line on what ad blocking did, with the busiest blocked hosts; "" if it was off."""
        if not self.block_ads:
            return ""
        blocked = sum(self.requests_blocked.values())
        top = ", ".join("{} x{}".format(h, n) for h, n in self.requests_blocked.most_common(8))
        skipped = ", ".join("{} x{}".format(k, n) for k, n in self.requests_skipped.items())
        return "Ad blocking: {} requests allowed, {} blocked ({} hosts). Busiest blocked: {}{}".format(
            sum(self.requests_allowed.values()), blocked, len(self.requests_blocked), top or "none",
            ". Also skipped on allowed hosts: " + skipped if skipped else "")

    def __exit__(self, exc_type, exc_value, traceback):
        summary = self.block_summary()
        if summary:
            print(summary)
        try:
            if self._persistent and self.context is not None:
                self.context.close()
            elif self.browser is not None:
                self.browser.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()

    def _pace_request(self, delays=REQUEST_DELAYS):
        """Add a small randomized pause before each outbound request, longer after blocks."""
        sleep(random.uniform(*delays) * self.slowdown)

    def _recent(self):
        if self.recent is None:
            self.recent = deque(maxlen=BLOCK_WINDOW)
        return self.recent

    def _note_block(self, what):
        """A rate limit or challenge happened: slow down, and stop if it keeps happening."""
        self.block_streak += 1
        self.clean_streak = 0
        self._recent().append(True)
        self.slowdown = min(self.slowdown * 2, MAX_SLOWDOWN)
        print("Blocked ({}); pauses are now {:g}x as long ({} in a row, limit {}).".format(
            what, self.slowdown, self.block_streak, MAX_CONSECUTIVE_BLOCKS), flush=True)
        if self.block_streak >= MAX_CONSECUTIVE_BLOCKS:
            raise TooManyBlocksError(
                "the site blocked {} requests in a row ({}); stopping so it is not pushed further. "
                "Progress is saved; wait an hour or more, then rerun".format(self.block_streak, what))
        if sum(self._recent()) >= MAX_BLOCKS_IN_WINDOW:
            # A challenge that clears by itself, again and again, never builds a streak.
            raise TooManyBlocksError(
                "the site blocked {} of the last {} requests; stopping so it is not pushed further. "
                "Progress is saved; wait an hour or more, then rerun".format(sum(self._recent()), len(self._recent())))

    def _note_ok(self):
        """A request went through: the streak is over, and long quiet stretches speed things up."""
        self.block_streak = 0
        self.clean_streak += 1
        self._recent().append(False)
        if self.slowdown > 1 and self.clean_streak >= CLEAN_FETCHES_TO_SPEED_UP:
            self.slowdown = max(1.0, self.slowdown / 2)
            self.clean_streak = 0
            print("No blocks for {} fetches; pauses are now {:g}x.".format(
                CLEAN_FETCHES_TO_SPEED_UP, self.slowdown), flush=True)

    @staticmethod
    def _rate_limit_wait(url, headers, operation):
        """Honor Retry-After, or use the five-minute fallback, plus a safety buffer."""
        retry_after = headers.get("retry-after")
        wait_seconds = RATE_LIMIT_COOLDOWN_SECONDS

        if retry_after:
            try:
                # Retry-After may be a number of seconds.
                wait_seconds = max(0.0, float(retry_after.strip()))
            except (AttributeError, TypeError, ValueError):
                try:
                    # Or it may be an HTTP-date.
                    retry_date = parsedate_to_datetime(retry_after)
                    if retry_date.tzinfo is None:
                        retry_date = retry_date.replace(tzinfo=timezone.utc)
                    wait_seconds = max(
                        0.0,
                        (retry_date - datetime.now(timezone.utc)).total_seconds(),
                    )
                except (TypeError, ValueError, OverflowError):
                    print(
                        "Invalid Retry-After header {!r}; using {}-second fallback.".format(
                            retry_after, RATE_LIMIT_COOLDOWN_SECONDS
                        ),
                        flush=True,
                    )

        if wait_seconds > MAX_RETRY_AFTER_SECONDS:
            raise TooManyBlocksError(
                "the site asked for a wait of {:.0f} seconds (HTTP 429 {} {}), longer than the "
                "{} seconds this crawler will wait; stopping. Progress is saved; try again later".format(
                    wait_seconds, operation, url, MAX_RETRY_AFTER_SECONDS))
        wait_seconds += RETRY_AFTER_BUFFER_SECONDS
        print(
            "HTTP 429 while {} {}. Waiting at least {:.1f} seconds before retrying.".format(
                operation, url, wait_seconds
            ),
            flush=True,
        )
        sleep(wait_seconds)

    scroll_complete = True   # False if the last fetch hit the scroll limit with the list still growing

    def _scroll_until_stable(self, item_selector, allow_partial=False):
        """Scroll a long page until the number of `item_selector` matches stops growing.

        Done when the page is at the bottom and the count has not grown for
        STABLE_SCROLL_CHECKS checks in a row. Raises ScrollIncompleteError if it is still
        growing after MAX_SCROLL_STEPS, because stopping there would silently drop items; with
        `allow_partial` it returns what it has and sets `scroll_complete` to False instead."""
        last_count, stable = -1, 0
        for _step in range(MAX_SCROLL_STEPS):
            count = self.page.locator(item_selector).count()
            at_bottom = self.page.evaluate(
                "() => window.scrollY + window.innerHeight >= "
                "document.documentElement.scrollHeight - 2"
            )
            stable = stable + 1 if (count == last_count and at_bottom) else 0
            if stable >= STABLE_SCROLL_CHECKS:
                self.page.evaluate("() => window.scrollTo(0, 0)")
                return count
            last_count = count
            self.page.evaluate(
                "() => window.scrollBy(0, Math.max(400, Math.floor(window.innerHeight * 0.8)))"
            )
            self.page.wait_for_timeout(SCROLL_STEP_WAIT_MS)
        if allow_partial:
            self.scroll_complete = False
            return last_count
        raise ScrollIncompleteError(
            "Page {} was still loading items after {} scroll steps ({} found); stopping instead "
            "of saving a partial list.".format(self.page.url, MAX_SCROLL_STEPS, last_count)
        )

    def _reached_heading(self, text):
        """True once a heading containing `text` (e.g. "All Reviews By Date") is in the viewport."""
        return self.page.evaluate(
            """(text) => [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].some(h =>
                h.textContent.toLowerCase().includes(text.toLowerCase())
                && h.getBoundingClientRect().top < window.innerHeight)""",
            text,
        )

    def _wait_for_lazy(self, selector, stop_at=None):
        """Scroll down a screen at a time until `selector` has been drawn by the page's own
        script, then give it a moment to fill in. Scrolling also stops once a heading
        containing `stop_at` (e.g. "Reviews") comes into view, since what we want is above
        it. Best effort: a page without the content carries on without it."""
        try:
            for _ in range(MAX_LAZY_STEPS):
                if self.page.locator(selector).count():
                    self.page.wait_for_timeout(1500)
                    return
                if stop_at and self._reached_heading(stop_at):
                    self.page.wait_for_timeout(1500)   # it may still be drawing
                    return
                self.page.evaluate("window.scrollBy(0, window.innerHeight)")
                self.page.wait_for_timeout(500)
        except PatchrightError:
            pass

    def _challenge_clears(self):
        """Wait up to CHALLENGE_WAIT_SECONDS for a challenge page to go away on its own.
        True if the page stopped looking like a challenge. Never interacts with it."""
        deadline = CHALLENGE_WAIT_SECONDS * 1000
        waited = 0
        while waited < deadline:
            self.page.wait_for_timeout(1000)
            waited += 1000
            try:
                if cloudflare_challenge_reason(self.page) is None:
                    return True
            except PatchrightError:
                return False
        return False

    def _fill_boxes(self, box_selector, filled_selector, stop_at=None, expect_boxes=0):
        """Bring each placeholder box (e.g. a vote card) into view and wait until the page's own
        script has filled it with numbers. Returns how many boxes there are; 0 means the page
        has none.

        The page may attach its scroll handling late, draw empty placeholder rows first, or
        only create the later boxes (the gender card) once the earlier ones are on screen. So
        "the rows exist" is not enough: a box counts as filled once `filled_selector` inside it
        contains a digit; each box is scrolled to again (with a small nudge, which makes the page
        notice) up to BOX_FILL_ATTEMPTS times; and the boxes are counted again after the last one
        in case more were created. If some box is still empty, a slow scroll down to the
        `stop_at` heading follows (the way a person reads the page). A box that never fills (a
        perfume nobody has voted on) is given up on, and the boxes after it get one short try."""
        try:
            self.page.wait_for_load_state("load", timeout=15000)   # scripts attached
        except PatchrightError:
            pass
        self._unlock_scrolling()
        boxes = self.page.locator(box_selector)
        attempts, index, unfilled, extra_steps, last_look = BOX_FILL_ATTEMPTS, 0, 0, 0, False
        while True:
            if index >= boxes.count():
                if expect_boxes and index >= expect_boxes:
                    break      # all the boxes a normal page has are there; no need to wait for more
                # Every known box is done. The page may create the next one (gender) only after
                # the reader has gone further down, so keep scrolling towards `stop_at` for a few
                # steps and look again after each.
                if extra_steps >= BOX_APPEAR_STEPS or (stop_at and self._reached_heading(stop_at)):
                    if last_look:
                        break
                    last_look = True          # one more look, up to a few seconds, then stop
                    known = boxes.count()
                    for _ in range(BOX_LAST_LOOK_MS // 250):
                        self.page.wait_for_timeout(250)
                        if boxes.count() > known:
                            break
                    continue
                extra_steps += 1
                self.page.evaluate("() => window.scrollBy(0, Math.floor(window.innerHeight * 0.9))")
                self.page.wait_for_timeout(BOX_APPEAR_WAIT_MS)
                continue
            box = boxes.nth(index)
            index += 1
            last_look = False   # a box was still being created; look again after this one
            for _ in range(attempts):
                try:
                    box.scroll_into_view_if_needed(timeout=5000)
                    self.page.evaluate("() => window.scrollBy(0, 40)")
                    self.page.evaluate("() => window.scrollBy(0, -40)")
                    handle = box.element_handle(timeout=2000)
                    self.page.wait_for_function(
                        "([el, sel]) => [...el.querySelectorAll(sel)].some(e => /\\d/.test(e.innerText))",
                        arg=[handle, filled_selector], timeout=BOX_FILL_WAIT_MS)
                    self.page.wait_for_timeout(300)   # let the rest of the rows finish
                    break
                except PatchrightError:
                    continue
            else:
                attempts = 1
                unfilled += 1
        count = boxes.count()
        if count and unfilled:
            self._slow_scroll_until(filled_selector, stop_at)
        return count

    def _unlock_scrolling(self):
        """Close a popup with Escape, and undo a scroll lock it left behind (see UNLOCK_SCROLL_JS)."""
        try:
            self.page.keyboard.press("Escape")
            self.page.evaluate(UNLOCK_SCROLL_JS)
        except PatchrightError:
            pass

    def _slow_scroll_until(self, filled_selector, stop_at):
        """Scroll from the top a step at a time, until every box holds numbers or `stop_at`
        comes into view or SLOW_SCROLL_STEPS is used up."""
        self._unlock_scrolling()
        try:
            self.page.evaluate("() => window.scrollTo(0, 0)")
            for _ in range(SLOW_SCROLL_STEPS):
                self.page.evaluate("() => window.scrollBy(0, Math.floor(window.innerHeight * 0.6))")
                self.page.wait_for_timeout(SLOW_SCROLL_WAIT_MS)
                if stop_at and self._reached_heading(stop_at):
                    self.page.wait_for_timeout(1500)
                    return
        except PatchrightError:
            pass

    def fetch_tree(self, url, timeout_ms=None, wait_for=None, scroll_for=None, stop_at=None,
                   lazy_boxes=None, expect_boxes=0, allow_partial=False):
        """Navigate, retry HTTP 429 after the required cooldown, and parse HTML.

        `wait_for`: CSS selector of content the page draws after it opens; scrolls only until
        it appears, or until a heading containing `stop_at` comes into view.
        `scroll_for`: CSS selector of the items in a long lazy-loaded list; scrolls until
        their number stops growing and raises if it never does.
        `expect_boxes`: how many lazy boxes a complete page has; once that many are filled the
        wait for further ones is skipped.
        `lazy_boxes`: CSS selector of placeholder boxes the page fills in when they are
        scrolled into view; each is brought into view and given time to fill (`wait_for` is the
        selector that appears inside a filled box). Pages without such boxes fall back to
        scrolling for `wait_for`.
        `allow_partial`: with `scroll_for`, a list still growing at the scroll limit is returned
        as it is and `scroll_complete` is set to False, instead of raising.

        HTTP 404/410 raise PageGoneError; 5xx is tried again SERVER_RETRIES times."""
        self.scroll_complete = True
        rate_limited = server_failed = 0
        while True:
            self._pace_request()
            response = self.page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_ms or self.timeout_ms,
            )
            if response is None:
                raise RuntimeError("No HTTP response received for {}".format(url))

            # Handle 429 before challenge and generic HTTP error checks.
            if response.status == 429:
                self._note_block("HTTP 429 loading {}".format(url))
                rate_limited += 1
                if rate_limited > MAX_RATE_LIMIT_RETRIES:
                    raise RuntimeError(
                        "HTTP 429 for {} after {} retries; the site is rate limiting this crawl".format(
                            url, MAX_RATE_LIMIT_RETRIES))
                self._rate_limit_wait(url, response.headers, "loading")
                continue

            challenge_reason = cloudflare_challenge_reason(self.page)
            if challenge_reason and self._challenge_clears():
                self._note_block("Cloudflare challenge before {}, cleared by itself".format(url))
                # The interstitial passed by itself, as it does in any browser that is left alone
                # for a few seconds. Nothing was clicked or changed; the page we now have is the
                # real one, so the status of the interstitial no longer applies.
                challenge_reason, response = None, None
            if challenge_reason:
                raise CloudflareChallengeError(
                    "{} while loading {} (HTTP {}; final URL: {}).".format(
                        challenge_reason, url, response.status, self.page.url
                    )
                )

            if response is not None and response.status in MISSING_PAGE_STATUSES:
                raise PageGoneError("HTTP {} for {}".format(response.status, url))
            if response is not None and response.status >= 500:
                server_failed += 1
                if server_failed > SERVER_RETRIES:
                    raise RuntimeError("HTTP {} for {} after {} retries".format(
                        response.status, url, SERVER_RETRIES))
                print("HTTP {} for {}; trying again in {} s.".format(
                    response.status, url, SERVER_RETRY_WAIT_SECONDS * server_failed), flush=True)
                sleep(SERVER_RETRY_WAIT_SECONDS * server_failed)
                continue
            if response is not None and response.status >= 400:
                raise RuntimeError("HTTP {} for {}".format(response.status, url))

            if scroll_for:
                self._scroll_until_stable(scroll_for, allow_partial)
            if wait_for:
                boxes = 0
                if lazy_boxes:
                    try:
                        boxes = self._fill_boxes(
                            lazy_boxes, wait_for.replace(lazy_boxes, "").strip() or wait_for, stop_at,
                            expect_boxes)
                    except PatchrightError:
                        boxes = 0
                if not boxes:
                    self._wait_for_lazy(wait_for, stop_at)
            self._note_ok()
            return html.fromstring(self.page.content())

    def download_image(self, image_url, destination):
        """Download an image, retrying HTTP 429 after the required cooldown.

        Returns True when the file is on disk afterwards. A picture that cannot be had (HTTP
        404/410, another error status, or a body that is not an image) is reported and skipped,
        returning False, so one broken picture does not stop a crawl. A 403, which may be a block,
        and anything else that goes wrong raises. The file is written under a temporary name and
        renamed, so a crash never leaves a truncated image that a later run would trust."""
        destination = Path(destination)
        if destination.exists():
            return True

        destination.parent.mkdir(parents=True, exist_ok=True)

        rate_limited = 0
        while True:
            self._pace_request(IMAGE_DELAYS)
            response = self.context.request.get(
                image_url,
                timeout=self.timeout_ms,
            )
            try:
                if response.status == 429:
                    self._note_block("HTTP 429 downloading {}".format(image_url))
                    rate_limited += 1
                    if rate_limited > MAX_RATE_LIMIT_RETRIES:
                        raise RuntimeError(
                            "HTTP 429 downloading image {} after {} retries".format(
                                image_url, MAX_RATE_LIMIT_RETRIES))
                    self._rate_limit_wait(image_url, response.headers, "downloading image")
                    continue

                if response.status == 403:
                    raise RuntimeError("HTTP 403 downloading image {}".format(image_url))

                if not response.ok:
                    print("Image not available (HTTP {}): {}".format(response.status, image_url), flush=True)
                    return False

                data = response.body()
                problem = image_problem(data)
                if problem:
                    print("Image skipped, {}: {}".format(problem, image_url), flush=True)
                    return False
                partial = destination.with_name(destination.name + ".part")
                partial.write_bytes(data)
                os.replace(partial, destination)
                self._note_ok()
                return True
            finally:
                response.dispose()
