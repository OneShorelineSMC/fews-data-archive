"""Shared helpers for the FEWS data archive (standard library only).

Integrity rules this module enforces:
  * Raw files are append-only. A reading is never changed or removed; a revised value (e.g. NOAA preliminary ->
    verified, a provisional value re-published) is added as a new version with its own retrieved_utc.
  * Values are stored exactly as the source published them (as text, no re-rounding).
  * Every reading carries its source (who, which sensor/product, how it was retrieved, when) and the source's
    own quality codes, untranslated, next to the common quality word.
  * Times are stored in UTC; the Pacific clock time is kept alongside, with a flag where the clock time was
    ambiguous (the repeated hour when daylight saving ends).
"""

import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
UTC = dt.timezone.utc
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

CREEKS_URL = os.environ.get(
    "CREEKS_URL", "https://raw.githubusercontent.com/OneShorelineSMC/oneshorelinefews/data/creeks.json")
NOAA = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
TIDE_STATIONS = {"9414523": "Redwood City", "9414750": "Alameda"}

RAW_COLS = ["sensor", "creek", "kind", "datetime_utc", "datetime_local", "time_flag", "value", "unit",
            "quality", "source_qualifier", "source", "source_id", "via", "retrieved_utc"]
# What makes two rows "the same version": a new row is added only if one of these differs
VERSION_KEY = ("value", "quality", "source_qualifier", "unit", "source_id")


# ---------- fetching ----------
def http_get(url, tries=4):
    if url.startswith("file://") or os.path.exists(url):
        with open(url.replace("file://", ""), "rb") as f:
            return f.read().decode("utf-8")
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "OneShoreline-FEWS-archive"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError):
            if i == tries - 1:
                raise
            time.sleep(10 * (i + 1))


def load_creeks(exact=False):
    """creeks.json. exact=True keeps every number as the text the site published (for raw archiving)."""
    txt = http_get(CREEKS_URL)
    return json.loads(txt, parse_float=str, parse_int=str) if exact else json.loads(txt)


# ---------- time ----------
def now_utc():
    return dt.datetime.now(UTC).replace(second=0, microsecond=0)


def fmt_utc(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def fmt_local(t):
    return t.astimezone(PT).strftime("%Y-%m-%d %H:%M")


def parse_utc(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)


def local_series_to_utc(times):
    """Pacific clock times in time order -> (UTC datetime, flag) each.

    When daylight saving ends, 1:00-1:59 AM happens twice. A series in time order that steps backwards within
    that hour has moved to the second pass (fold=1). Those readings, and any that can't be told apart, are
    flagged 'dst_repeated_hour'; clock times that don't exist (spring forward) are flagged 'dst_skipped_hour'."""
    out, prev = [], None
    for s in times:
        naive = dt.datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
        a = naive.replace(tzinfo=PT, fold=0)
        b = naive.replace(tzinfo=PT, fold=1)
        ua, ub = a.astimezone(UTC), b.astimezone(UTC)
        flag = ""
        if ua.astimezone(PT).replace(tzinfo=None) != naive:   # clock time that doesn't exist (spring forward)
            u, flag = ua, "dst_skipped_hour"
        elif ua != ub:                                         # repeated hour (fall back)
            flag = "dst_repeated_hour"
            u = ub if (prev is not None and ua <= prev) else ua
        else:
            u = ua
        out.append((u, flag))
        prev = u
    return out


# ---------- raw files ----------
def raw_path(day):
    return os.path.join(ROOT, "raw", f"{day:%Y}", f"{day:%m}", f"{day:%Y-%m-%d}.csv.gz")


def read_raw(day):
    """Every version of every reading for one Pacific day."""
    p = raw_path(day)
    if not os.path.exists(p):
        return []
    with gzip.open(p, "rt", newline="") as f:
        return list(csv.DictReader(f))


def merge_versions(old, new):
    """Append-only merge: keep every old row; add a new row only if it is a new version of that reading."""
    seen = {}
    for r in old:
        seen.setdefault((r["sensor"], r["datetime_utc"]), set()).add(tuple(r.get(k, "") for k in VERSION_KEY))
    added = []
    for r in new:
        k = (r["sensor"], r["datetime_utc"])
        v = tuple(r.get(x, "") for x in VERSION_KEY)
        if v not in seen.setdefault(k, set()):
            seen[k].add(v)
            added.append(r)
    return old + added, added


def write_raw(day, rows, previous=None):
    """Write a day file. Refuses to drop or change any row that was already archived."""
    if previous:
        have = {tuple(r.get(c, "") for c in RAW_COLS) for r in rows}
        lost = [r for r in previous if tuple(r.get(c, "") for c in RAW_COLS) not in have]
        if lost:
            raise RuntimeError(f"{day}: refusing to write - {len(lost)} archived row(s) would be lost")
    p = raw_path(day)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["sensor"], r["datetime_utc"], r.get("retrieved_utc", "")))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=RAW_COLS, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    with open(p, "wb") as f:
        f.write(gzip.compress(buf.getvalue().encode("utf-8"), mtime=0))   # mtime=0: identical bytes if unchanged


def current(rows):
    """Current best value per reading: the most recently retrieved version."""
    best = {}
    for r in rows:
        k = (r["sensor"], r["datetime_utc"])
        if k not in best or r.get("retrieved_utc", "") >= best[k].get("retrieved_utc", ""):
            best[k] = r
    return sorted(best.values(), key=lambda r: (r["sensor"], r["datetime_utc"]))


def read_raw_range(start_day, end_day, versions="current"):
    rows = []
    d = start_day
    while d <= end_day:
        rows += read_raw(d)
        d += dt.timedelta(days=1)
    return rows if versions == "all" else current(rows)


def raw_days():
    base = os.path.join(ROOT, "raw")
    days = []
    for _, _, files in os.walk(base):
        days += [dt.date.fromisoformat(fn[:10]) for fn in files if fn.endswith(".csv.gz")]
    return sorted(days)


# ---------- metadata and versions ----------
def sensor_meta():
    p = os.path.join(ROOT, "meta", "sensors.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def short_hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:10]


def code_version():
    """The archive code's git commit (what produced a flag)."""
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"][:10]
    try:
        return subprocess.check_output(["git", "-C", ROOT, "rev-parse", "--short=10", "HEAD"],
                                       stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return "local"


def append_csv(path, cols, rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
