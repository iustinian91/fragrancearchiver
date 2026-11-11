"""Shared Patchright browser helpers for the FragranceArchiver crawlers."""

import os
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import sleep
from urllib.parse import urljoin, urlsplit

from lxml import html
from patchright.sync_api import Error as PatchrightError
from patchright.sync_api import sync_playwright

BASE_URL = "https://www.fragrantica.com"
SITE_HOSTS = {urlsplit(BASE_URL).netloc, urlsplit(BASE_URL).netloc.removeprefix("www.")}
REQUEST_TIMEOUT_MS = 60000
REQUEST_DELAYS = (2.5, 10.0)
IMAGE_DELAYS = (0.5, 1.5)  # images come from the CDN right after a page; no need to wait as long
CHALLENGE_WAIT_SECONDS = 20   # how long a Cloudflare "checking your browser" page may take to clear itself
MAX_RATE_LIMIT_RETRIES = 5
RATE_LIMIT_COOLDOWN_SECONDS = 300
RETRY_AFTER_BUFFER_SECONDS = 2
SCROLL_STEP_WAIT_MS = 500
STABLE_SCROLL_CHECKS = 3
MAX_SCROLL_STEPS = 150
MAX_LAZY_STEPS = 20
BOX_FILL_WAIT_MS = 1500      # how long a placeholder box gets to fill in once it is on screen
BOX_FILL_ATTEMPTS = 4        # how many times a box is scrolled to again before giving up on it
BOX_APPEAR_WAIT_MS = 700     # after the last known box, how long to wait (per step) for another to be created
BOX_LAST_LOOK_MS = 2500      # at the end of the page, how long a late box may still take to appear
BOX_APPEAR_STEPS = 24        # how many such steps down the page before concluding there are no more boxes
SLOW_SCROLL_STEPS = 12       # the slow second pass when a box stayed empty
SLOW_SCROLL_WAIT_MS = 600
HEADLESS = False


class ScrollIncompleteError(RuntimeError):
    """Raised when a lazy-loaded list is still growing when the scroll limit is reached."""


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


MISSING_IMAGE_STATUSES = (404, 410)
IMAGE_SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8", b"RIFF")  # jpeg, png, gif, webp


# A popup or ad overlay may lock the page's scrolling (body {overflow: hidden}); undo that so the
# page can be scrolled to read what is on it. Nothing is clicked or removed.
UNLOCK_SCROLL_JS = """() => {
  for (const el of [document.documentElement, document.body]) {
    if (el && getComputedStyle(el).overflowY === 'hidden') el.style.setProperty('overflow', 'visible', 'important');
  }
}"""


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


class BrowserClient:
    """Own a visible Patchright Chromium browser and its page/context."""

    def __init__(self, headless=HEADLESS, timeout_ms=REQUEST_TIMEOUT_MS):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def __enter__(self):
        self._playwright = sync_playwright().start()
        try:
            self.browser = self._playwright.chromium.launch(headless=self.headless)
            self.context = self.browser.new_context()
            self.page = self.context.new_page()
            self.page.set_default_timeout(self.timeout_ms)
        except BaseException:
            # __exit__ is not called when __enter__ fails; don't leak the driver process.
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self.browser is not None:
                self.browser.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()

    @staticmethod
    def _pace_request(delays=REQUEST_DELAYS):
        """Add a small randomized pause before each outbound request."""
        sleep(random.uniform(*delays))

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

        wait_seconds += RETRY_AFTER_BUFFER_SECONDS
        print(
            "HTTP 429 while {} {}. Waiting at least {:.1f} seconds before retrying.".format(
                operation, url, wait_seconds
            ),
            flush=True,
        )
        sleep(wait_seconds)

    def _scroll_until_stable(self, item_selector):
        """Scroll a long page until the number of `item_selector` matches stops growing.

        Done when the page is at the bottom and the count has not grown for
        STABLE_SCROLL_CHECKS checks in a row. Raises ScrollIncompleteError if it is still
        growing after MAX_SCROLL_STEPS, because stopping there would silently drop items."""
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
                   lazy_boxes=None, expect_boxes=0):
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
        scrolling for `wait_for`."""
        for _attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
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
                if _attempt < MAX_RATE_LIMIT_RETRIES:
                    self._rate_limit_wait(url, response.headers, "loading")
                continue

            challenge_reason = cloudflare_challenge_reason(self.page)
            if challenge_reason and self._challenge_clears():
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

            if response is not None and response.status >= 400:
                detail = "HTTP {} for {}".format(response.status, url)
                raise RuntimeError(detail)

            if scroll_for:
                self._scroll_until_stable(scroll_for)
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
            return html.fromstring(self.page.content())
        raise RuntimeError(
            "HTTP 429 for {} after {} retries; the site is rate limiting this crawl".format(
                url, MAX_RATE_LIMIT_RETRIES
            )
        )

    def download_image(self, image_url, destination):
        """Download an image, retrying HTTP 429 after the required cooldown.

        Returns True when the file is on disk afterwards. A picture the site no longer has
        (404/410) is reported and skipped, returning False, so one broken link does not stop a
        crawl. Anything else that goes wrong raises. The file is written under a temporary name
        and renamed, so a crash never leaves a truncated image that a later run would trust."""
        destination = Path(destination)
        if destination.exists():
            return True

        destination.parent.mkdir(parents=True, exist_ok=True)

        for _attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            self._pace_request(IMAGE_DELAYS)
            response = self.context.request.get(
                image_url,
                timeout=self.timeout_ms,
            )
            try:
                if response.status == 429:
                    if _attempt < MAX_RATE_LIMIT_RETRIES:
                        self._rate_limit_wait(
                            image_url, response.headers, "downloading image"
                        )
                    continue

                if response.status in MISSING_IMAGE_STATUSES:
                    print("Image not available (HTTP {}): {}".format(response.status, image_url), flush=True)
                    return False

                if not response.ok:
                    raise RuntimeError(
                        "HTTP {} downloading image {}".format(
                            response.status, image_url
                        )
                    )

                data = response.body()
                problem = image_problem(data)
                if problem:
                    raise RuntimeError("Bad image from {}: {}".format(image_url, problem))
                partial = destination.with_name(destination.name + ".part")
                partial.write_bytes(data)
                os.replace(partial, destination)
                return True
            finally:
                response.dispose()
        raise RuntimeError(
            "HTTP 429 downloading image {} after {} retries".format(
                image_url, MAX_RATE_LIMIT_RETRIES
            )
        )
