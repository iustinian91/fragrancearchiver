"""Independent readers and comparisons for smoke_test.py.

The scrapers read a page's HTML with lxml. The "oracle" here reads what the browser actually
shows (rendered text, visible bar widths) with its own JavaScript, so a scraper bug cannot hide
behind the same bug in the check. Everything that decides PASS/FAIL is a plain function over
dicts, so it can be tested without a browser (tests/test_smoke.py).
"""

import re

PASS, FAIL, UNAVAILABLE = "PASS", "FAIL", "UNAVAILABLE"

# What each chart is called on the page, in order. Longevity and sillage are stored under older
# names by position (see perfumes_scraper.page_charts); gender and when-to-wear keep their labels.
SITE_LABELS = {
    "longevity": ("very weak", "weak", "moderate", "long lasting", "eternal"),
    "sillage": ("intimate", "moderate", "strong", "enormous"),
    "gender": ("female", "more female", "unisex", "more male", "male"),
    "when_to_wear": ("winter", "spring", "summer", "fall", "day", "night"),
}
STORED_KEYS = {
    "longevity": ("poor", "weak", "moderate", "long lasting", "very long lasting"),
    "sillage": ("soft", "moderate", "heavy", "enormous"),
    "gender": SITE_LABELS["gender"],
    "when_to_wear": SITE_LABELS["when_to_wear"],
}
RELATIVE_TO_MAX = {"when_to_wear"}
BAR_TOLERANCE = 0.03   # displayed bars use exact counts, ours use the site's rounded ones

PERFUME_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const text = e => clean(e && e.innerText);
  const isCount = s => /^[\d.,]+\s*[kKmM]?$/.test(s);
  const out = {title: '', brand: '', year: null, accords: [], notes: {}, charts: {}, images: {}};

  const h1 = document.querySelector('h1');
  out.title = text(h1);
  const brand = [...document.querySelectorAll('a[href*="/designers/"][href$=".html"]')].map(text).find(t => t);
  out.brand = brand || '';
  const launched = clean(document.body.innerText).match(/launched in (\d{4})/i);
  const endYear = clean(document.title).match(/(?:^|\D)((?:1[5-9]|20)\d\d)\s*$/);
  out.year = launched ? launched[1] : (endYear ? endYear[1] : null);

  // main accords: the labels under the "main accords" heading, in the order shown
  const accHead = [...document.querySelectorAll('h6')].find(h => /^main accords$/i.test(text(h)));
  if (accHead && accHead.nextElementSibling) {
    out.accords = [...accHead.nextElementSibling.querySelectorAll('span.truncate')]
      .map(text).filter(t => t);
  }

  // notes: walk the pyramid in document order; each note label belongs to the closest
  // "Top/Middle/Base Notes" heading above it. A page without those headings shows one flat list.
  let level = null;
  document.querySelectorAll('h4, .pyramid-note-label').forEach(el => {
    if (el.tagName === 'H4') {
      const m = text(el).toLowerCase().match(/^(top|middle|base) notes?/);
      if (m) level = m[1];
    } else {
      const t = text(el);
      if (t) (out.notes[level || 'general'] = out.notes[level || 'general'] || []).push(t);
    }
  });

  // vote charts as displayed: label, displayed count text, displayed bar width
  const rowsIn = box => {
    const rows = [];
    const spans = [...box.querySelectorAll('span')].filter(s => text(s));
    spans.forEach((s, i) => {
      const t = text(s);
      if (!isCount(t) || i === 0) return;
      const label = text(spans[i - 1]);
      if (isCount(label) || /^no vote$/i.test(label)) return;
      const row = s.closest('[index]') || (s.parentElement && s.parentElement.parentElement);
      const bar = row && [...row.querySelectorAll('[style*="width"]')]
        .map(b => b.getAttribute('style').match(/width:\s*([\d.]+)%/)).find(m => m);
      rows.push({label: label.toLowerCase(), count: t, width: bar ? parseFloat(bar[1]) : null});
    });
    return rows;
  };
  document.querySelectorAll('.tw-perf-card').forEach(card => {
    const head = text(card.querySelector('[class*="uppercase"]')).toLowerCase();
    if (head) out.charts[head] = rowsIn(card);
  });
  const when = [...document.querySelectorAll('span')].find(s => /^when to wear$/i.test(text(s)));
  if (when) {
    let box = when;
    while (box && !box.querySelector('[index]')) box = box.parentElement;
    out.charts['when to wear'] = box ? rowsIn(box) : [];
  }

  const og = [...document.querySelectorAll('meta[property="og:image"]')].map(m => m.content);
  out.images = {og: og, photo: og.find(u => u.includes('/mdimg/perfume/o.')) || null};
  return out;
}
"""

NOTE_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  // the note's own photos (/mdimg/sastojci/...), not the perfume thumbnails further down
  const imgs = [...document.querySelectorAll('img')]
    .filter(i => i.offsetParent !== null && /\/mdimg\/sastojci\/[a-z]\d*\.\d+\.jpe?g/i.test(i.getAttribute('src') || ''))
    .map(i => new URL(i.getAttribute('src'), location.href).pathname.split('/').pop());
  return {title: clean((document.querySelector('h1') || {}).innerText), images: [...new Set(imgs)]};
}
"""

SCROLL_UNTIL_CHARTS_JS = "() => !!document.querySelector('.tw-perf-card [class*=\"space-y-2\"]')"

DESIGNER_LINKS_JS = r"""
() => new Set([...document.querySelectorAll('a[href]')]
  .map(a => { try { return new URL(a.getAttribute('href'), location.href); } catch (e) { return null; } })
  .filter(u => u && /\/perfume\/[^/]+\/[^/]+-\d+\.html?$/i.test(u.pathname))
  .map(u => u.pathname)).size
"""

NOTE_LINKS_JS = r"""
() => new Set([...document.querySelectorAll('a[href]')]
  .map(a => { try { return new URL(a.getAttribute('href'), location.href); } catch (e) { return null; } })
  .filter(u => u && /\/notes\/[^/]+-\d+\.html?$/i.test(u.pathname))
  .map(u => u.pathname)).size
"""


def parse_count(text):
    """'216' -> 216, '2.6k' -> 2600, '1.2M' -> 1200000 (the same abbreviation rule as the site)."""
    match = re.fullmatch(r"([\d.,]+)\s*([kKmM]?)", " ".join(str(text).split()))
    if not match:
        return None
    factor = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2).lower()]
    try:
        return round(float(match.group(1).replace(",", "")) * factor)
    except ValueError:
        return None


def _check(field, status, detail=""):
    return {"field": field, "status": status, "detail": detail}


def _norm(text):
    return " ".join((text or "").split()).casefold()


# ---------------------------------------------------------------- perfume page comparisons
def compare_identity(stored, shown):
    """Title, brand and launch year against what the page displays."""
    checks = []
    title, h1 = stored["title"], shown["title"]
    # The page heading reads "<perfume> <brand> for women" (the brand and audience are small print).
    ok = _norm(h1).startswith(_norm(title)) and re.fullmatch(
        r"\s*{}\s*(for (women and men|women|men))?\s*".format(re.escape(_norm(stored["brand"]))),
        _norm(h1)[len(_norm(title)):])
    checks.append(_check("title", PASS if ok else FAIL, "stored {!r}; page heading {!r}".format(title, h1)))
    checks.append(_check(
        "title from page (not URL)", PASS if stored["title_source"] == "page" else FAIL,
        "title_source={}".format(stored["title_source"])))

    same_brand = _norm(stored["brand"]) == _norm(shown["brand"])
    checks.append(_check("brand", PASS if same_brand else FAIL,
                         "stored {!r}; page {!r}".format(stored["brand"], shown["brand"])))
    checks.append(_check(
        "brand from page (not URL)", PASS if stored["brand_source"] == "page" else FAIL,
        "brand_source={}".format(stored["brand_source"])))

    if shown["year"] is None and stored["year"] is None:
        checks.append(_check("year", UNAVAILABLE, "year unknown: none on the page"))
    else:
        checks.append(_check("year", PASS if stored["year"] == shown["year"] else FAIL,
                             "stored {!r}; page {!r}".format(stored["year"], shown["year"])))
    return checks


def compare_accords(stored, shown):
    if not shown["accords"] and not stored:
        return [_check("accords", UNAVAILABLE, "none shown on the page")]
    return [_check("accords", PASS if (stored or []) == shown["accords"] else FAIL,
                   "stored {}; page {}".format(stored, shown["accords"]))]


def compare_notes(stored, shown):
    """Top, middle and base notes must be in the groups the page puts them in."""
    shown_notes = shown["notes"]
    if not shown_notes and not stored:
        return [_check("notes", UNAVAILABLE, "no notes shown on the page")]
    checks = []
    stored = stored or {}
    if not shown_notes and any(stored.values()):
        return [_check("notes", FAIL, "stored {}; the page shows no notes".format(stored))]
    if set(shown_notes) <= {"top", "middle", "base"}:
        for level in ("top", "middle", "base"):
            want, got = shown_notes.get(level) or [], stored.get(level) or []
            checks.append(_check(
                "notes: " + level,
                (PASS if got == want else FAIL) if want or got else UNAVAILABLE,
                "stored {}; page {}".format(got, want)))
    else:  # no pyramid: one flat list
        want = [n for level in shown_notes.values() for n in level]
        got = stored.get("general") or [n for level in stored.values() for n in (level or [])]
        checks.append(_check("notes: general", PASS if sorted(set(got)) == sorted(set(want)) else FAIL,
                             "stored {}; page {}".format(got, want)))
    return checks


def compare_chart(name, stored_votes, shown_rows):
    """Stored vote counts against the displayed categories and counts."""
    field = "chart: " + name
    labels = tuple(r["label"] for r in shown_rows)
    if not shown_rows:
        status = UNAVAILABLE if stored_votes is None else FAIL
        return [_check(field, status, "page shows no votes; stored {}".format(stored_votes))]
    counts = [parse_count(r["count"]) for r in shown_rows]
    if labels != SITE_LABELS[name]:
        return [_check(field, FAIL, "page labels {} are not the expected {}".format(
            list(labels), list(SITE_LABELS[name])))]
    if not any(counts):
        return [_check(field, PASS if stored_votes is None else FAIL, "all displayed counts are 0")]
    if stored_votes is None:
        return [_check(field, FAIL, "page shows {} but nothing was stored".format(dict(zip(labels, counts))))]
    got = [int(stored_votes.get(key, -1)) for key in STORED_KEYS[name]]
    detail = "displayed {}; stored {}".format(
        ", ".join("{}={}".format(lab, r["count"]) for lab, r in zip(labels, shown_rows)),
        ", ".join("{}={}".format(k, v) for k, v in zip(STORED_KEYS[name], got)))
    return [_check(field, PASS if got == counts else FAIL, detail)]


def compare_normalized(name, cleaned, shown_rows):
    """The cleaned chart's shares against the votes it came from and the bars the page draws."""
    field = "normalized: " + name
    if cleaned is None:
        return [_check(field, UNAVAILABLE, "no chart")]
    votes, share = cleaned["votes"], cleaned["share"]
    keys = STORED_KEYS[name]
    base = max(votes.values()) if name in RELATIVE_TO_MAX else sum(votes.values())
    problems = []
    for key in keys:
        if abs(share[key] - round(votes[key] / base, 4)) > 1e-9:
            problems.append("{} share {} != {}/{}".format(key, share[key], votes[key], base))
    total = max(share.values()) if name in RELATIVE_TO_MAX else sum(share.values())
    if abs(total - 1) > 0.002:
        problems.append("shares {} {}".format("peak at" if name in RELATIVE_TO_MAX else "add up to", total))
    drawn = [r["width"] for r in shown_rows]
    if len(drawn) == len(keys) and all(w is not None for w in drawn):
        for key, width in zip(keys, drawn):
            if abs(share[key] - width / 100) > BAR_TOLERANCE:
                problems.append("{} share {} vs bar {}%".format(key, share[key], width))
    return [_check(field, FAIL if problems else PASS, "; ".join(problems) or "shares match the votes and the drawn bars")]


def compare_image(stored_name, url_id, shown, file_problem):
    photo = shown["images"]["photo"]
    expected = "o.{}.jpg".format(url_id)
    checks = [
        _check("image url", PASS if photo and photo.endswith("/mdimg/perfume/" + expected) else FAIL,
               "page photo {!r}; expected .../{}".format(photo, expected)),
        _check("image name", PASS if stored_name == expected else FAIL,
               "stored {!r}; expected {!r}".format(stored_name, expected)),
        _check("image file", FAIL if file_problem else PASS, file_problem or "downloaded, non-empty and decodes"),
    ]
    return checks


def compare_perfume(record, shown, cleaned_charts, file_problem, url_id):
    """All field checks for one perfume. `record` is the stored row as a dict with decoded JSON."""
    info = record["perfume"]
    checks = compare_identity(
        {"title": info["title"], "brand": record["brand"], "year": record["launch_year"],
         "title_source": info.get("title_source"), "brand_source": info.get("brand_source")}, shown)
    checks += compare_accords(record["main_accords"], shown)
    checks += compare_notes(record["notes"], shown)
    for name in SITE_LABELS:
        rows = shown["charts"].get(name.replace("_", " "), [])
        checks += compare_chart(name, record[name], rows)
        checks += compare_normalized(name, (cleaned_charts or {}).get(name), rows)
    checks += compare_image(info.get("image"), url_id, shown, file_problem)
    return checks


# ---------------------------------------------------------------- images
def image_problem(path):
    """None if `path` is a real, non-trivial, decodable image; otherwise what is wrong with it."""
    from pathlib import Path
    path = Path(path)
    if not path.exists():
        return "file is missing"
    data = path.read_bytes()
    if len(data) < 1024:
        return "file is only {} bytes".format(len(data))
    kinds = (data[:3] == b"\xff\xd8\xff", data[:8] == b"\x89PNG\r\n\x1a\n",
             data[:4] == b"RIFF" and data[8:12] == b"WEBP", data[:4] == b"GIF8")
    if not any(kinds):
        return "not a JPEG/PNG/WEBP/GIF (starts with {!r})".format(data[:8])
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
    except ImportError:
        if kinds[0] and not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
            return "JPEG is cut off (no end marker)"
    except Exception as exc:   # PIL raises many kinds of errors for corrupt files
        return "does not decode: {}".format(exc)
    return None


# ---------------------------------------------------------------- notes pages
def compare_note(title, image_names, shown, problems_by_name):
    """A note page: its title, and that the stored image names are exactly the photos the page
    shows, in order, each downloaded as a real image."""
    checks = []
    h1 = shown["title"]
    checks.append(_check("title", PASS if title and _norm(title) == _norm(h1) else FAIL,
                         "stored {!r}; page heading {!r}".format(title, h1)))
    if not image_names and not shown["images"]:
        checks.append(_check("images", UNAVAILABLE, "the page shows no photos"))
        return checks
    # the page may give photo URLs or bare file names; compare file names (the query is a cache-buster)
    page_names = [str(u).split("?")[0].rsplit("/", 1)[-1] for u in shown["images"]]
    checks.append(_check("image names", PASS if list(image_names) == page_names else FAIL,
                         "stored {}; page shows {}".format(list(image_names), page_names)))
    bad = {n: p for n, p in problems_by_name.items() if p}
    checks.append(_check("image files", FAIL if bad or not image_names else PASS,
                         "; ".join("{}: {}".format(n, p) for n, p in bad.items()) or
                         "{} image(s) downloaded and valid".format(len(image_names))))
    return checks
