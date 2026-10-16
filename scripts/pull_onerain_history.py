"""
OneRain history from the public sensor pages (no login, no export, no API).

Reads the same public page anyone can open on oneshorelineearlywarning.onerain.com, with a custom date range, in
week-long pieces. If a piece has more readings than the page limit, it is split in half until each piece fits, so
nothing is silently cut off.

    python scripts/pull_onerain_history.py --select belmont --start 2022-12-01 --end 2023-01-31 --probe

--select  creek ids (belmont), gauge keys (bel_stage), 'stage' or 'rain' (every gauge of that type), or 'all'
--probe   also check how far back each sensor's history goes (one request per year)

Output (out/onerain_history/):
  <gauge>.csv  datetime_utc, datetime_local (to the second), time_flag, value (exactly as shown on the page), unit
  report.md    what was read: pages, readings, any pieces that couldn't be read completely, history reach

Gauge IDs come from the live site's lookups/creek_gauges.csv.
"""

import argparse
import csv
import datetime as dt
import html as htmllib
import http.cookiejar
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
UTC = dt.timezone.utc
SITE = "https://oneshorelineearlywarning.onerain.com"
GAUGES_URL = os.environ.get(
    "GAUGES_URL", "https://raw.githubusercontent.com/OneShorelineSMC/oneshorelinefews/main/lookups/creek_gauges.csv")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OneShoreline-FEWS-archive/1.0; "
                  "+https://github.com/OneShorelineSMC/fews-data-archive)",
    "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
STAMP = r"(\d{4}-\d\d-\d\d \d\d:\d\d(?::\d\d)?)"
NUMBER = r"(-?\d+(?:\.\d+)?)"
MAX_PAGES = 40          # pages per request before we call the piece "too big" and split it
CAP_READINGS = 190      # OneRain's public pages stop at 200 readings per request, silently (no "more" link).
                        # A piece that returns this many may be cut off, so it is split and re-read.
PAUSE_S = 1.0           # between requests, to be polite to OneRain
PIECE_DAYS = 7
MIN_PIECE = dt.timedelta(hours=3)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "out", "onerain_history")


class Session:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.requests = 0

    def get(self, url, tries=4):
        for i in range(tries):
            try:
                time.sleep(PAUSE_S)
                self.requests += 1
                with self.opener.open(urllib.request.Request(url, headers=HEADERS), timeout=90) as r:
                    return r.read().decode("utf-8", errors="replace")
            except (urllib.error.URLError, TimeoutError) as e:
                if i == tries - 1:
                    raise
                print(f"    retry after {type(e).__name__}", file=sys.stderr)
                time.sleep(15 * (i + 1))


def read_gauges():
    if os.path.exists(GAUGES_URL):
        txt = open(GAUGES_URL).read()
    else:
        with urllib.request.urlopen(urllib.request.Request(GAUGES_URL, headers=HEADERS), timeout=60) as r:
            txt = r.read().decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(txt)))
    return [r for r in rows if (r.get("source") or "onerain").strip().lower() != "usgs"
            and r.get("site_id", "").strip().isdigit() and r.get("device_id", "").strip().isdigit()]


def sensor_page(g, start, end):
    return SITE + "/sensor/?" + urllib.parse.urlencode({
        "site_id": g["site_id"], "site": g["site"].lower(), "device_id": g["device_id"], "device": g["device"].lower(),
        "data_start": start.strftime("%Y-%m-%d %H:%M:%S"), "data_end": end.strftime("%Y-%m-%d %H:%M:%S"),
        "range": "Custom Range", "time_zone": "US/Pacific"})


def text_number(fragment):
    """The number exactly as written on the page (kept as text)."""
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))
    m = re.search(NUMBER, text)
    return m.group(1) if m else None


def unit_of(fragment):
    text = " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())
    m = re.search(NUMBER + r"\s*([A-Za-z]+)", text)
    return m.group(2) if m else ""


def parse_readings(page):
    m = re.search(r'id="scrollable-readings"(.*?)</ul>', page, flags=re.S)
    if not m:
        return [], ""
    out, unit = [], ""
    for item in re.split(r'<li class="list-group-item', m.group(1))[1:]:
        t = re.search(r'<span class="text-nowrap">\s*' + STAMP + r"\s*</span>", item)
        heads = re.findall(r'<h4 class="list-group-item-heading">(.*?)</h4>', item, flags=re.S)
        if not t or len(heads) < 2:
            continue
        v = text_number(heads[1])
        if v is not None:
            unit = unit or unit_of(heads[1])
            s = t.group(1)
            out.append((dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S" if len(s) > 16 else "%Y-%m-%d %H:%M"), v))
    return out, unit


def next_page(page):
    for pat in (r'<a[^>]*class="[^"]*\bnext\b[^"]*"[^>]*href="([^"]+)"',
                r'<a[^>]*href="([^"]+)"[^>]*class="[^"]*\bnext\b',
                r'class="[^"]*\bnext\b[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"'):
        m = re.search(pat, page)
        if m:
            return urllib.parse.urljoin(SITE + "/sensor/", htmllib.unescape(m.group(1)))
    return None


def to_utc(local, prev, descending):
    """Pacific clock time -> UTC; in the repeated November hour, keep page order."""
    a, b = (local.replace(tzinfo=TZ, fold=f).astimezone(UTC) for f in (0, 1))
    if a == b:
        return a, ""
    if descending:
        return (b if b <= prev else a), "dst_repeated_hour"
    return (a if a >= prev else b), "dst_repeated_hour"


def read_piece(session, g, start, end):
    """All readings in [start, end) for one gauge. Returns (readings{utc: (local, value, flag)}, pages, complete)."""
    url, pages, seen, readings, prev, desc, unit = sensor_page(g, start, end), 0, set(), {}, None, True, ""
    while url and url not in seen:
        if pages >= MAX_PAGES:
            return readings, pages, False, unit
        seen.add(url)
        page = session.get(url)
        pages += 1
        batch, u = parse_readings(page)
        unit = unit or u
        if not batch:
            break
        if prev is None:
            desc = len(batch) < 2 or batch[0][0] >= batch[-1][0]
            prev = (end if desc else start).replace(tzinfo=TZ, fold=1 if desc else 0).astimezone(UTC)
        for t, v in batch:
            ut, flag = to_utc(t, prev, desc)
            prev = ut
            if start <= t < end:
                readings[ut] = (t, v, flag)
        if (desc and min(t for t, _ in batch) < start) or (not desc and max(t for t, _ in batch) >= end):
            break
        url = next_page(page)
    return readings, pages, len(readings) < CAP_READINGS, unit


def read_range(session, g, start, end, log):
    """Week by week; a piece too big for MAX_PAGES is split in half until it fits."""
    out, unit, todo = {}, "", []
    t = start
    while t < end:
        todo.append((t, min(t + dt.timedelta(days=PIECE_DAYS), end)))
        t += dt.timedelta(days=PIECE_DAYS)
    while todo:
        a, b = todo.pop(0)
        r, pages, complete, u = read_piece(session, g, a, b)
        unit = unit or u
        if not complete and b - a > MIN_PIECE:
            mid = a + (b - a) / 2
            log.append(f"| {g['gauge']} | {a:%Y-%m-%d %H:%M} – {b:%Y-%m-%d %H:%M} | {pages} | too many readings: split |")
            todo[:0] = [(a, mid), (mid, b)]
            continue
        out.update(r)
        log.append(f"| {g['gauge']} | {a:%Y-%m-%d %H:%M} – {b:%Y-%m-%d %H:%M} | {pages} | {len(r)} readings"
                   f"{'' if complete else ' — INCOMPLETE (could not split further)'} |")
    return out, unit


def probe(session, g, years):
    """One request per calendar year (first page only): does the public page have any readings that year?"""
    res = []
    for y in years:
        page = session.get(sensor_page(g, dt.datetime(y, 1, 1), dt.datetime(y + 1, 1, 1)))
        batch, _ = parse_readings(page)
        res.append((y, len(batch)))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--select", default="belmont")
    ap.add_argument("--start", default="2022-12-01", help="YYYY-MM-DD, Pacific")
    ap.add_argument("--end", default="2023-01-31", help="YYYY-MM-DD, Pacific, inclusive")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--probe-only", action="store_true", help="only check how far back each gauge goes")
    ap.add_argument("--list", action="store_true", help="print the matching gauge keys as JSON and stop")
    a = ap.parse_args()

    want = {w.strip() for w in a.select.split(",") if w.strip()}
    gauges = [g for g in read_gauges() if "all" in want or g["creek"] in want or g["gauge"] in want
              or g.get("type", "").lower() in want]          # 'stage' or 'rain' = every gauge of that type
    if not gauges:
        sys.exit(f"No OneRain gauges match {a.select!r}")
    if a.list:
        print(json.dumps([g["gauge"] for g in gauges]))
        return
    t_start = time.time()
    start = dt.datetime.fromisoformat(a.start)
    end = dt.datetime.fromisoformat(a.end) + dt.timedelta(days=1)
    os.makedirs(OUT, exist_ok=True)
    session, log, summary, reach = Session(), [], [], []
    retrieved = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    for g in gauges:
        print(f"{g['gauge']} ({g['onerain_number']} {g['type']}): {a.start} to {a.end}", flush=True)
        readings, unit = ({}, "") if a.probe_only else read_range(session, g, start, end, log)
        # to the second, as the page shows it: a rain gauge can log two tips in the same minute
        rows = [{"datetime_utc": u.strftime("%Y-%m-%dT%H:%M:%SZ"), "datetime_local": t.strftime("%Y-%m-%d %H:%M:%S"),
                 "time_flag": f, "value": v, "unit": unit, "source_id":
                 f"onerain {g['onerain_number']} site_id={g['site_id']} device_id={g['device_id']}",
                 "via": "onerain public sensor page", "retrieved_utc": retrieved}
                for u, (t, v, f) in sorted(readings.items())]
        if a.probe_only:
            p = probe(session, g, range(2008, dt.date.today().year + 1))
            reach.append(f"| {g['gauge']} | " + " | ".join(str(n) for _, n in p) + " |")
            years = [y for y, _ in p]
            continue
        with open(os.path.join(OUT, f"{g['gauge']}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["datetime_utc"], lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        gaps = 0
        for x, y in zip(rows, rows[1:]):
            if (dt.datetime.strptime(y["datetime_utc"][:19], "%Y-%m-%dT%H:%M:%S")
                    - dt.datetime.strptime(x["datetime_utc"][:19], "%Y-%m-%dT%H:%M:%S")) > dt.timedelta(hours=13):
                gaps += 1
        summary.append(f"| {g['gauge']} | {g['type']} | {len(rows)} | {rows[0]['datetime_local'] if rows else '–'} | "
                       f"{rows[-1]['datetime_local'] if rows else '–'} | {unit} | {gaps} |")
        print(f"  {len(rows)} readings", flush=True)
        if a.probe:
            p = probe(session, g, range(2008, dt.date.today().year + 1))
            reach.append(f"| {g['gauge']} | " + " | ".join(str(n) for _, n in p) + " |")
            years = [y for y, _ in p]

    with open(os.path.join(OUT, "report.md"), "w") as f:
        f.write(f"# OneRain history pull\n\nSelection `{a.select}`, {a.start} to {a.end} (Pacific). "
                f"Read {retrieved} from the public sensor pages; {session.requests} requests in "
                f"{(time.time() - t_start) / 60:.1f} min ({(time.time() - t_start) / max(1, session.requests):.1f} s each).\n\n")
        f.write("## Readings\n\n| Gauge | Type | Readings | First | Last | Unit | Silences > 13 h |\n|---|---|---|---|---|---|---|\n")
        f.write("\n".join(summary) + "\n\n")
        if reach:
            f.write("## How far back (readings found in each year; first page only, so 100 = 'plenty')\n\n| Gauge | " + " | ".join(map(str, years))
                    + " |\n|---|" + "---|" * len(years) + "\n" + "\n".join(reach) + "\n\n")
        f.write("## Pieces read\n\n| Gauge | Range | Pages | Result |\n|---|---|---|---|\n" + "\n".join(log) + "\n")
    print(open(os.path.join(OUT, "report.md")).read())


if __name__ == "__main__":
    main()
