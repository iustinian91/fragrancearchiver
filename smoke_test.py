"""Small live test of every scraper, run before a full extraction.

    python smoke_test.py [--country Thailand] [--designers 3] [--notes 8] [--screenshots]
    python smoke_test.py --only-failed smoke_run_<earlier time>      # re-test just the pages that failed
    python smoke_test.py --urls my_urls.txt                          # re-test only these perfume pages

It uses the real browser and the live site, but only a handful of pages, and writes everything
into its own new folder (default smoke_run_<time>/): separate databases, manifests and images.
Your real perfumes.sqlite, notes.sqlite, app.db and manifests are never opened for writing, and the
full scrapers are never started; their entry points are called with trial-run limits. A
fingerprint of your real data before and after proves it.

Each field is compared with what the live page displays, read by an independent reader
(smoke_oracle.py) rather than by the scraper's own parsing. The result is a PASS / FAIL /
UNAVAILABLE line per field, per page (UNAVAILABLE = the page shows nothing and nothing was
stored). The run stops at the first sign of blocking (HTTP 429/403, a challenge page, a list
that never stops loading) and does not try to get around it.

Exit status: 0 all passed, 1 something failed, 2 blocked.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

import smoke_oracle as so  # noqa: E402
from browser_client import BASE_URL, UNLOCK_SCROLL_JS, BrowserClient  # noqa: E402

PASS, FAIL, UNAVAILABLE, BLOCKED = so.PASS, so.FAIL, so.UNAVAILABLE, "BLOCKED"
BLOCK_SIGNS = re.compile(r"429|403|challenge|rate limit|still loading items|Cloudflare", re.IGNORECASE)
PERFUME_URL = re.compile(r"https://www\.fragrantica\.com/perfume/[^/?#]+/[^/?#]+-(\d+)\.html")
PRODUCTION = ("perfumes.sqlite", "notes.sqlite", "app.db", "perfumes.json", "perfumes_clean.json",
              "perfumes_manifest.txt", "perfumes_manifest.txt.tmp", "perfumes_manifest.state.json",
              "notes_manifest.txt")


class Blocked(Exception):
    """Access was refused or a challenge appeared; the test stops and reports it."""


class Report:
    def __init__(self):
        self.rows = []

    def add(self, stage, subject, field, status, detail=""):
        self.rows.append({"stage": stage, "subject": subject, "field": field,
                          "status": status, "detail": detail})
        mark = {PASS: "  ok  ", UNAVAILABLE: " n/a  ", FAIL: " FAIL ", BLOCKED: "BLOCK "}[status]
        if status != PASS or not subject:
            print("[{}] {} {} {}{}".format(mark, stage, subject, field, " -- " + detail if detail else ""))

    def add_all(self, stage, subject, checks):
        for c in checks:
            self.add(stage, subject, c["field"], c["status"], c["detail"])

    def counts(self):
        return {s: sum(1 for r in self.rows if r["status"] == s) for s in (PASS, UNAVAILABLE, FAIL, BLOCKED)}


class Tee(io.TextIOBase):
    """Show output as it happens and keep a copy for the checks."""

    def __init__(self):
        self.text = ""

    def write(self, data):
        self.text += data
        sys.__stdout__.write(data)
        return len(data)

    def flush(self):
        sys.__stdout__.flush()


def run_main(main, argv):
    """Run a scraper's main(); return (exit code, what it printed). Raises Blocked on a block."""
    tee = Tee()
    with contextlib.redirect_stdout(tee):
        code = main(argv)
    if code and BLOCK_SIGNS.search(tee.text):
        raise Blocked(tee.text.strip().splitlines()[-1] if tee.text.strip() else "blocked")
    return code, tee.text


def fingerprint():
    state = {}
    for name in PRODUCTION:
        path = REPO / name
        state[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    for folder in ("fragrantica_images",):
        path = REPO / folder
        state[folder] = sum(1 for _ in path.rglob("*")) if path.exists() else None
    return state


def read_urls(path):
    return list(dict.fromkeys(ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()))


def curated_urls():
    urls = []
    for line in (REPO / "smoke_urls.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            urls.append(BASE_URL + line)
    return urls


def url_id(url):
    return re.search(r"-(\d+)\.html?$", url).group(1)


def patient_scroll(page, done_js=None, count_js=None, max_steps=150):
    """Scroll slowly (more patiently than the scrapers) until `done_js` is true, or until
    `count_js` has stopped changing at the bottom of the page. Returns the last count."""
    last, stable = None, 0
    for _ in range(max_steps):
        page.evaluate(UNLOCK_SCROLL_JS)   # a popup may lock scrolling; the scrapers undo that too
        if done_js and page.evaluate(done_js):
            page.wait_for_timeout(2500)
            return None
        count = page.evaluate(count_js) if count_js else None
        at_bottom = page.evaluate(
            "() => window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2")
        stable = stable + 1 if (count_js and count == last and at_bottom) else 0
        if stable >= 6:
            return count
        last = count
        page.evaluate("() => window.scrollBy(0, Math.floor(window.innerHeight * 0.7))")
        page.wait_for_timeout(700)
    return last


def save_failure(workdir, name, page):
    try:
        folder = workdir / "failures"
        folder.mkdir(exist_ok=True)
        (folder / (name + ".html")).write_text(page.content(), encoding="utf-8")
    except Exception as exc:   # a failure artifact must never hide the real failure
        print("(could not save the failure page: {})".format(exc))


# ---------------------------------------------------------------------------- stages
def stage_access(report):
    with BrowserClient() as client:
        tree = client.fetch_tree(BASE_URL + "/")
        title = " ".join(tree.xpath("//title/text()")).strip()
        links = tree.xpath("//a[contains(@href, '/perfume') or contains(@href, '/designers')]")
    report.add("access", "", "site loads without a block", PASS if title and links else FAIL,
               "title {!r}, {} perfume/designer links".format(title, len(links)))


def stage_perfume_manifest(report, args, workdir):
    import perfumes_extract_manifest as em
    stage = "perfume URLs"
    code, out = run_main(em.main, ["--country", args.country, "--designers", str(args.designers)])
    report.add(stage, "", "crawl finished", PASS if code == 0 else FAIL, "exit code {}".format(code))
    manifest = workdir / "perfumes_manifest.txt"
    if code or not manifest.exists():
        return []
    urls = read_urls(manifest)
    ids = [url_id(u) for u in urls if PERFUME_URL.fullmatch(u)]
    report.add(stage, "", "URLs found", PASS if urls else FAIL, "{} URLs".format(len(urls)))
    bad = [u for u in urls if not PERFUME_URL.fullmatch(u)]
    report.add(stage, "", "every URL is canonical (https, www, no query)", FAIL if bad else PASS, "; ".join(bad[:3]))
    report.add(stage, "", "one URL per perfume id", PASS if len(ids) == len(set(ids)) else FAIL,
               "{} URLs, {} ids".format(len(ids), len(set(ids))))

    designers = re.findall(r"\[Designer (\d+)/(\d+)\] (\S+)", out)
    found = [int(n) for n in re.findall(r"\[Saved\] (\d+) perfumes found", out)]
    wanted = min(args.designers, int(designers[0][1])) if designers else 0
    report.add(stage, "", "designers crawled", PASS if designers and len(designers) == wanted else FAIL,
               "{} of {} asked for".format(len(designers), args.designers))
    if not args.skip_oracle and designers and len(found) == len(designers):
        with BrowserClient() as client:
            for (_, _, designer), scraped in zip(designers, found):
                client.fetch_tree(designer)
                shown = patient_scroll(client.page, count_js=so.DESIGNER_LINKS_JS)
                status = PASS if shown == scraped and scraped > 0 else FAIL
                report.add(stage, designer.rsplit("/", 1)[-1], "perfumes found == perfumes displayed",
                           status, "crawler {}; page (patient scroll) {}".format(scraped, shown))
                if status == FAIL:
                    save_failure(workdir, "designer_" + designer.rsplit("/", 1)[-1], client.page)
    return urls


def stage_notes_manifest(report, args, workdir):
    import notes_extract_manifest as nem
    stage = "notes URLs"
    code, _ = run_main(lambda argv: nem.main(), [])
    manifest = workdir / "notes_manifest.txt"
    report.add(stage, "", "manifest written", PASS if code == 0 and manifest.exists() else FAIL, "exit code {}".format(code))
    if code or not manifest.exists():
        return []
    urls = read_urls(manifest)
    bad = [u for u in urls if not u.startswith(BASE_URL + "/notes/")]
    report.add(stage, "", "note URLs look right", PASS if urls and not bad else FAIL,
               "{} URLs; unexpected: {}".format(len(urls), bad[:3]))
    if not args.skip_oracle:
        with BrowserClient() as client:
            client.fetch_tree(BASE_URL + "/notes/")
            shown = patient_scroll(client.page, count_js=so.NOTE_LINKS_JS)
            report.add(stage, "", "notes found == notes displayed", PASS if shown == len(urls) else FAIL,
                       "manifest {}; page {}".format(len(urls), shown))
    return urls


def table_rows(db, table, columns):
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT {} FROM {}".format(columns, table)).fetchall()
    progress = [r[0] for r in conn.execute("SELECT url FROM scrape_progress")]
    conn.close()
    return rows, progress


def db_checks(report, stage, db, urls, rows, key_of):
    report.add(stage, "", "one record per URL", PASS if len(rows) == len(urls) else FAIL,
               "{} records for {} URLs".format(len(rows), len(urls)))
    keys = [key_of(r) for r in rows]
    report.add(stage, "", "no duplicate records", PASS if len(keys) == len(set(keys)) else FAIL,
               "{} records, {} distinct".format(len(keys), len(set(keys))))


def idempotence(report, stage, main, argv, db, table, workdir, manifest, urls):
    before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]
    code, out = run_main(main, argv)
    after = sqlite3.connect(db).execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]
    ok = code == 0 and before == after and re.search(r"\b0 (to scrape now|remaining)", out)
    report.add(stage, "", "re-run adds nothing and fetches nothing", PASS if ok else FAIL,
               "records {} -> {}; exit {}".format(before, after, code))
    manifest.write_text("\n".join(urls + urls) + "\n", encoding="utf-8")   # every line twice
    run_main(main, argv)
    again = sqlite3.connect(db).execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]
    manifest.write_text("\n".join(urls) + "\n", encoding="utf-8")
    report.add(stage, "", "duplicate manifest lines create no duplicate records", PASS if again == before else FAIL,
               "records {} -> {}".format(before, again))


def failed_urls(previous_run):
    """Perfume page URLs that had a FAIL in an earlier smoke run's report, in the order tested."""
    previous = Path(previous_run)
    checks = json.loads((previous / "report.json").read_text(encoding="utf-8"))["checks"]
    failed = {c["subject"] for c in checks if c["stage"] == "perfume pages" and c["status"] == FAIL and c["subject"]}
    tested = [ln.strip() for ln in (previous / "perfumes_manifest.txt").read_text(encoding="utf-8").splitlines() if ln.strip()]
    urls = [u for u in tested if u.rsplit("/", 1)[-1] in failed]
    missing = failed - {u.rsplit("/", 1)[-1] for u in urls}
    if missing:
        print("Could not find these failed pages in {}: {}".format(previous, ", ".join(sorted(missing))))
    return urls


def stage_perfume_scrape(report, args, workdir, crawl_urls):
    import clean
    import export
    import perfumes_scraper as ps
    stage = "perfume pages"
    if args.only:
        sample = list(args.only)
    else:
        sample = curated_urls()
        sample += [u for u in crawl_urls if u not in sample][:args.from_crawl]
    manifest = workdir / "perfumes_manifest.txt"
    if manifest.exists():
        manifest.replace(workdir / "perfumes_manifest.crawl.txt")
    manifest.write_text("\n".join(sample) + "\n", encoding="utf-8")

    code, _ = run_main(ps.main, ["--db", "perfumes.sqlite"])
    report.add(stage, "", "scraper finished", PASS if code == 0 else FAIL, "exit code {}".format(code))
    if code:
        return
    columns = "brand, perfume, launch_year, main_accords, notes, longevity, sillage, gender, when_to_wear"
    raw, progress = table_rows("perfumes.sqlite", "perfumes", columns)
    names = columns.split(", ")
    records = []
    for row in raw:
        rec = dict(zip(names, row))
        for key in ("perfume", "main_accords", "notes", "longevity", "sillage", "gender", "when_to_wear"):
            rec[key] = json.loads(rec[key]) if rec[key] is not None else None
        records.append(rec)
    by_id = {}
    for rec in records:
        m = re.match(r"o\.(\d+)\.", (rec["perfume"] or {}).get("image") or "")
        if m:
            by_id[m.group(1)] = rec

    report.add(stage, "", "progress ledger matches the manifest", PASS if sorted(progress) == sorted(sample) else FAIL,
               "{} marked complete for {} URLs".format(len(progress), len(sample)))
    db_checks(report, stage, "perfumes.sqlite", sample, records, lambda r: (r["perfume"] or {}).get("image"))
    idempotence(report, stage, ps.main, ["--db", "perfumes.sqlite"], "perfumes.sqlite", "perfumes", workdir, manifest, sample)

    export.export_to_json("perfumes.sqlite", "perfumes.json")
    clean.clean("perfumes.json", "perfumes_clean.json")
    cleaned = {c["image"]: c["charts"] for c in json.loads(Path("perfumes_clean.json").read_text(encoding="utf-8"))}

    def verify_page(url, shown, page):
        rec = by_id.get(url_id(url))
        subject = url.rsplit("/", 1)[-1]
        if rec is None:
            report.add(stage, subject, "stored record found", FAIL, "no record has image id {}".format(url_id(url)))
            return
        image = rec["perfume"].get("image")
        problem = so.image_problem(ps.IMAGE_DIR / image) if image else "no image name stored"
        checks = so.compare_perfume(rec, shown, cleaned.get(image), problem, url_id(url))
        report.add_all(stage, subject, checks)
        if any(c["status"] == FAIL for c in checks):
            save_failure(workdir, subject.replace(".html", ""), page)

    if args.skip_oracle:
        return
    with BrowserClient() as client:
        for number, url in enumerate(sample):
            client.fetch_tree(url)
            patient_scroll(client.page, done_js=so.SCROLL_UNTIL_CHARTS_JS, max_steps=60)
            shown = client.page.evaluate(so.PERFUME_JS)
            verify_page(url, shown, client.page)
            if args.screenshots and number < args.screenshots:
                shoot(client.page, workdir / "screenshots", url_id(url))


def shoot(page, folder, name):
    """Element screenshots of the vote charts, so a person can compare them with the stored values."""
    folder.mkdir(exist_ok=True)
    try:
        cards = page.locator(".tw-perf-card")
        for i in range(cards.count()):
            cards.nth(i).screenshot(path=str(folder / "{}_card{}.png".format(name, i + 1)))
        page.get_by_text("When To Wear", exact=True).first.locator(
            "xpath=ancestor::div[.//div[@index]][1]").screenshot(path=str(folder / "{}_when.png".format(name)))
    except Exception as exc:   # screenshots are a convenience, never a reason to fail
        print("screenshot skipped for {}: {}".format(name, exc))


def stage_notes_scrape(report, args, workdir, all_notes):
    import notes_scraper as ns
    stage = "note pages"
    if not all_notes:
        report.add(stage, "", "note pages scraped", FAIL, "no notes manifest to sample")
        return
    step = max(1, len(all_notes) // args.notes)
    sample = all_notes[::step][:args.notes]
    manifest = workdir / "notes_manifest.txt"
    manifest.replace(workdir / "notes_manifest.full.txt")
    manifest.write_text("\n".join(sample) + "\n", encoding="utf-8")

    code, _ = run_main(ns.main, ["--db", "notes.sqlite"])
    report.add(stage, "", "scraper finished", PASS if code == 0 else FAIL, "exit code {}".format(code))
    if code:
        return
    rows, progress = table_rows("notes.sqlite", "notes", "*")
    report.add(stage, "", "progress ledger matches the manifest", PASS if sorted(progress) == sorted(sample) else FAIL,
               "{} marked complete for {} URLs".format(len(progress), len(sample)))
    db_checks(report, stage, "notes.sqlite", sample, rows, lambda r: (r[0], r[1]))
    idempotence(report, stage, ns.main, ["--db", "notes.sqlite"], "notes.sqlite", "notes", workdir, manifest, sample)

    if args.skip_oracle:
        return
    stored = {r[0]: json.loads(r[1]) for r in rows}
    with BrowserClient() as client:
        for url in sample:
            client.fetch_tree(url)
            client.page.wait_for_timeout(2000)
            shown = client.page.evaluate(so.NOTE_JS)
            title = next((t for t in stored if " ".join(t.split()).casefold() == " ".join(shown["title"].split()).casefold()), None)
            names = stored.get(title, [])
            problems = {n: so.image_problem(ns.IMAGE_DIR / n) for n in names}
            checks = so.compare_note(title, names, shown, problems)
            subject = url.rstrip("/").rsplit("/", 1)[-1]
            report.add_all(stage, subject, checks)
            if any(c["status"] == FAIL for c in checks):
                save_failure(workdir, "note_" + subject.replace(".html", ""), client.page)


def stage_pipeline(report):
    import build_app_db
    stage = "pipeline"
    build_app_db.main()
    conn = sqlite3.connect("app.db")
    cleaned = json.loads(Path("perfumes_clean.json").read_text(encoding="utf-8"))
    count = conn.execute("SELECT COUNT(*) FROM perfumes").fetchone()[0]
    report.add(stage, "", "app.db has every perfume", PASS if count == len(cleaned) else FAIL,
               "{} in app.db, {} cleaned".format(count, len(cleaned)))
    report.add(stage, "", "database integrity", PASS if conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok" else FAIL)
    bad = conn.execute("SELECT COUNT(*) FROM perfume_votes WHERE share < 0 OR share > 1 OR votes < 0").fetchone()[0]
    report.add(stage, "", "vote shares between 0 and 1", PASS if bad == 0 else FAIL, "{} out of range".format(bad))
    kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM notes")}
    report.add(stage, "", "notes saved in top/middle/base/general groups",
               PASS if kinds and kinds <= {"top", "middle", "base", "general"} else FAIL,
               "groups found: {}".format(", ".join(sorted(kinds)) or "none"))
    wrong = conn.execute("""SELECT COUNT(*) FROM perfumes p WHERE p.popularity !=
        COALESCE((SELECT SUM(votes) FROM perfume_votes v WHERE v.perfume_id = p.id AND v.chart = 'longevity'), 0)""").fetchone()[0]
    report.add(stage, "", "popularity == longevity votes", PASS if wrong == 0 else FAIL, "{} differ".format(wrong))
    fts = conn.execute("SELECT COUNT(*) FROM perfume_fts").fetchone()[0]
    report.add(stage, "", "search index covers every perfume", PASS if fts == count else FAIL, "{} indexed".format(fts))
    conn.close()
    result = subprocess.run([sys.executable, str(REPO / "verify_scrape.py"), "--db", "perfumes.sqlite",
                             "--manifest", "perfumes_manifest.txt"], capture_output=True, text=True)
    (Path("verify_scrape.txt")).write_text(result.stdout + result.stderr, encoding="utf-8")
    report.add(stage, "", "verify_scrape.py", PASS if result.returncode == 0 else FAIL,
               "exit {}; see verify_scrape.txt".format(result.returncode))


# ---------------------------------------------------------------------------- driver
def write_report(report, workdir, started):
    counts = report.counts()
    (workdir / "report.json").write_text(json.dumps(
        {"started": started, "counts": counts, "checks": report.rows}, indent=1), encoding="utf-8")
    lines = ["# Smoke test report", "", "Started {}.  PASS {}  UNAVAILABLE {}  FAIL {}  BLOCKED {}".format(
        started, counts[PASS], counts[UNAVAILABLE], counts[FAIL], counts[BLOCKED]), ""]
    stage = None
    for r in report.rows:
        if r["stage"] != stage:
            stage = r["stage"]
            lines += ["", "## " + stage, "", "| status | page | check | detail |", "|---|---|---|---|"]
        lines.append("| {} | {} | {} | {} |".format(r["status"], r["subject"], r["field"], r["detail"].replace("|", "/")))
    (workdir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--country", default="Thailand", help="country for the URL crawl (default %(default)s)")
    parser.add_argument("--designers", type=int, default=3, help="designers to crawl in it (default %(default)s)")
    parser.add_argument("--notes", type=int, default=8, help="note pages to scrape (default %(default)s)")
    parser.add_argument("--from-crawl", type=int, default=3, dest="from_crawl",
                        help="perfumes taken from the crawl itself, on top of smoke_urls.txt (default %(default)s)")
    parser.add_argument("--screenshots", type=int, default=0, metavar="N",
                        help="save chart screenshots for the first N perfume pages, for a visual check")
    parser.add_argument("--skip-oracle", action="store_true",
                        help="skip the comparison with the live page (halves the requests; weaker test)")
    parser.add_argument("--only-failed", metavar="RUN_FOLDER", dest="only_failed",
                        help="re-test only the perfume pages that failed in an earlier smoke_run folder")
    parser.add_argument("--urls", metavar="FILE",
                        help="re-test only the perfume page URLs listed in FILE (one per line)")
    parser.add_argument("--workdir", help="new folder for this run (default smoke_run_<time>)")
    args = parser.parse_args(argv)
    args.only = None
    if args.only_failed:
        args.only = failed_urls(args.only_failed)
        if not args.only:
            print("Nothing to re-test: no perfume page failed in {}.".format(args.only_failed))
            return 0
    elif args.urls:
        args.only = [ln.split("#")[0].strip() for ln in Path(args.urls).read_text(encoding="utf-8").splitlines()]
        args.only = [u if u.startswith("http") else "https://www.fragrantica.com" + u for u in args.only if u]

    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    workdir = Path(args.workdir or "smoke_run_" + datetime.now().strftime("%Y%m%d_%H%M%S")).resolve()
    if workdir == REPO or workdir in REPO.parents or (workdir.exists() and any(workdir.iterdir())):
        print("Refusing to use {}: pick a new or empty folder that is not the project folder.".format(workdir))
        return 1
    workdir.mkdir(parents=True, exist_ok=True)
    before = fingerprint()
    os.chdir(workdir)
    print("Smoke test in {}\n".format(workdir))

    report, outcome = Report(), 0
    try:
        stage_access(report)
        crawl = [] if args.only else stage_perfume_manifest(report, args, workdir)
        stage_perfume_scrape(report, args, workdir, crawl)
        if not args.only:    # a re-test of chosen perfume pages leaves the notes alone
            notes = stage_notes_manifest(report, args, workdir)
            stage_notes_scrape(report, args, workdir, notes)
        stage_pipeline(report)
    except Blocked as exc:
        report.add("blocked", "", "access was refused or a challenge appeared; stopped", BLOCKED, str(exc))
        outcome = 2
    except Exception as exc:   # a crash in the test itself is a failure, not a pass
        if BLOCK_SIGNS.search(str(exc)):
            report.add("blocked", "", "access was refused or a challenge appeared; stopped", BLOCKED, str(exc))
            outcome = 2
        else:
            report.add("test run", "", "completed without crashing", FAIL, "{}: {}".format(type(exc).__name__, exc))

    untouched = fingerprint() == before
    report.add("safety", "", "your real data files and images are unchanged", PASS if untouched else FAIL)
    write_report(report, workdir, started)

    counts = report.counts()
    print("\n=== PASS {}   UNAVAILABLE {}   FAIL {}   BLOCKED {} ===".format(
        counts[PASS], counts[UNAVAILABLE], counts[FAIL], counts[BLOCKED]))
    print("Report: {}/report.md".format(workdir))
    if (workdir / "failures").exists():
        print("Pages that failed a check were saved in {}/failures; send them if a selector needs fixing.".format(workdir))
    return outcome or (1 if counts[FAIL] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
