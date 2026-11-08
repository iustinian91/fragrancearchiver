"""Shared Patchright browser helpers for the FragranceArchiver crawlers."""

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import sleep

from lxml import html
from patchright.sync_api import Error as PatchrightError, sync_playwright

BASE_URL = "https://www.fragrantica.com"
REQUEST_TIMEOUT_MS = 60000
REQUEST_DELAYS = (2.5, 10.0)
IMAGE_DELAYS = (0.5, 1.5)  # images come from the CDN right after a page; no need to wait as long
MAX_RATE_LIMIT_RETRIES = 5
RATE_LIMIT_COOLDOWN_SECONDS = 300
RETRY_AFTER_BUFFER_SECONDS = 2
SCROLL_STEP_WAIT_MS = 500
STABLE_SCROLL_CHECKS = 3
MAX_SCROLL_STEPS = 150
MAX_LAZY_STEPS = 20
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

    def fetch_tree(self, url, timeout_ms=None, wait_for=None, scroll_for=None, stop_at=None):
        """Navigate, retry HTTP 429 after the required cooldown, and parse HTML.

        `wait_for`: CSS selector of content the page draws after it opens; scrolls only until
        it appears, or until a heading containing `stop_at` comes into view. `scroll_for`: CSS selector of the items in a long lazy-loaded list; scrolls
        until their number stops growing and raises if it never does."""
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
            if challenge_reason:
                raise CloudflareChallengeError(
                    "{} while loading {} (HTTP {}; final URL: {}).".format(
                        challenge_reason, url, response.status, self.page.url
                    )
                )

            if response.status >= 400:
                detail = "HTTP {} for {}".format(response.status, url)
                raise RuntimeError(detail)

            if scroll_for:
                self._scroll_until_stable(scroll_for)
            if wait_for:
                self._wait_for_lazy(wait_for, stop_at)
            return html.fromstring(self.page.content())
        raise RuntimeError(
            "HTTP 429 for {} after {} retries; the site is rate limiting this crawl".format(
                url, MAX_RATE_LIMIT_RETRIES
            )
        )

    def download_image(self, image_url, destination):
        """Download an image, retrying HTTP 429 after the required cooldown."""
        destination = Path(destination)
        if destination.exists():
            return

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

                if not response.ok:
                    raise RuntimeError(
                        "HTTP {} downloading image {}".format(
                            response.status, image_url
                        )
                    )

                destination.write_bytes(response.body())
                return
            finally:
                response.dispose()
        raise RuntimeError(
            "HTTP 429 downloading image {} after {} retries".format(
                image_url, MAX_RATE_LIMIT_RETRIES
            )
        )
