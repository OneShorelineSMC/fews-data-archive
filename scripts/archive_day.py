"""
Archive: save every reading from the live FEWS data (creeks.json) and NOAA tides into one compressed file per
Pacific day, raw/YYYY/MM/YYYY-MM-DD.csv.gz. Append-only: a reading is never changed or removed; a revised value
is added as a new version (see common.py for the rules and RAW_COLS for the columns).

Also maintains:
  meta/sensors.json   the current sensor list (creek, kind, units, thresholds, Atlas 14 storm sizes, source)
  meta/sources.csv    a row whenever a sensor's source details, units, thresholds or storm sizes change
  meta/runs.csv       one row per run: when, what was fetched, how many readings and versions were added

Health check: the run FAILS (so GitHub emails the repo admins) if the live data is older than STALE_HOURS or if
nothing could be read, after saving whatever it did get.

Usage:  python scripts/archive_day.py            # the last 2 complete days + today
        python scripts/archive_day.py --days 6   # if the live site publishes a longer window
"""

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse

from common import (CREEKS_URL, NOAA, PT, ROOT, TIDE_STATIONS, UTC, append_csv, code_version, fmt_local, fmt_utc,
                    http_get, local_series_to_utc, merge_versions, now_utc, raw_path, read_raw, short_hash,
                    write_raw)

STALE_HOURS = 6


def source_id_of(s, src):
    """Who and which sensor, specifically."""
    if src == "usgs":
        param = "00065" if s.get("kind", "stage") == "stage" else ""
        return f"usgs {s.get('usgs_site') or str(s.get('number', '')).replace('USGS ', '')} {param}".strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(s.get("url", "")).query)
    sid, dev = q.get("site_id", [""])[0], q.get("device_id", [""])[0]
    return f"onerain {s.get('number', '')} site_id={sid} device_id={dev}".strip()


def creek_rows(cj_exact, cj, retrieved):
    meta = {}
    for c in cj.get("creeks", []) + cj.get("creeks_preview", []):
        for role in ("stages", "rain", "rain_support"):
            for s in c.get(role, []):
                src = s.get("source", "onerain")
                meta[s["key"]] = {
                    "creek": c["id"], "creek_name": c.get("name"), "preview": bool(c.get("preview_only")),
                    "role": role, "label": s.get("label"), "number": s.get("number"), "source": src,
                    "source_id": source_id_of(dict(s, kind="stage" if role == "stages" else "rain"), src),
                    "url": s.get("url"), "reports": s.get("reports"), "tidal": s.get("tidal"),
                    "thresholds": s.get("thresholds", []), "tide_station": c.get("tide")}
        for t in c.get("rain_triggers", []):
            meta.setdefault(t["gauge"], {}).setdefault("rain_triggers", []).append(t)
    rows = []
    for key, s in cj_exact.get("sensors", {}).items():
        sp = cj["sensors"][key]
        m = meta.setdefault(key, {"source": "onerain", "source_id": "onerain"})
        m.update(name=sp.get("name"), kind=sp.get("kind"), unit=sp.get("unit"),
                 freq=(sp.get("signals") or {}).get("freq"))
        pts = [p for p in s.get("points", []) if p and p[1] is not None]
        # a point may carry its own UTC time as a 3rd element, or its local time with an offset (if the site publishes it)
        times = local_series_to_utc([p[0] for p in pts])
        for p, (u, flag) in zip(pts, times):
            if len(p) > 2 and isinstance(p[2], str) and p[2].endswith("Z"):
                u, flag = dt.datetime.fromisoformat(p[2].replace("Z", "+00:00")), ""
            elif len(p[0]) > 16 and p[0][16:17] in "+-Z":           # local time published with its offset
                u, flag = dt.datetime.fromisoformat(p[0].replace(" ", "T").replace("Z", "+00:00")), ""
            rows.append({"sensor": key, "creek": m.get("creek", ""), "kind": sp.get("kind"),
                         "datetime_utc": fmt_utc(u), "datetime_local": fmt_local(u), "time_flag": flag,
                         "value": p[1], "unit": sp.get("unit", ""), "quality": "provisional",
                         "source_qualifier": "", "source": m["source"], "source_id": m["source_id"],
                         "via": "fews creeks.json", "retrieved_utc": retrieved})
    return rows, meta


def tide_rows(start_utc, end_utc, retrieved):
    rows, problems = [], []
    for st, name in TIDE_STATIONS.items():
        for product, kind in (("water_level", "tide_measured"), ("predictions", "tide_predicted")):
            q = {"product": product, "station": st, "begin_date": start_utc.strftime("%Y%m%d %H:%M"),
                 "end_date": end_utc.strftime("%Y%m%d %H:%M"), "datum": "NAVD", "units": "english",
                 "time_zone": "gmt", "format": "json", "application": "OneShoreline_FEWS_archive"}
            if product == "predictions":
                q["interval"] = "6"
            try:
                doc = json.loads(http_get(NOAA + "?" + urllib.parse.urlencode(q)), parse_float=str)
            except Exception as e:
                problems.append(f"NOAA {product} {st}: {e}")
                continue
            if "error" in doc:
                problems.append(f"NOAA {product} {st}: {doc['error']}")
                continue
            for p in doc.get("data") or doc.get("predictions") or []:
                if p.get("v") in (None, ""):
                    continue
                u = dt.datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
                if kind == "tide_measured":
                    quality = {"v": "verified", "p": "preliminary"}.get(p.get("q", ""), "unknown")
                    qual = f"q={p.get('q', '')} f={p.get('f', '')} s={p.get('s', '')}"
                else:
                    quality, qual = "predicted", ""
                rows.append({"sensor": f"tide_{st}_{kind.split('_')[1]}", "creek": "", "kind": kind,
                             "datetime_utc": fmt_utc(u), "datetime_local": fmt_local(u), "time_flag": "",
                             "value": str(p["v"]), "unit": "ft NAVD88", "quality": quality, "source_qualifier": qual,
                             "source": "noaa", "source_id": f"noaa {st} {product} {name}", "via": "noaa api",
                             "retrieved_utc": retrieved})
    return rows, problems


def log_source_changes(meta, retrieved):
    """meta/sources.csv: append a row for any sensor whose source details changed since its last row."""
    p = os.path.join(ROOT, "meta", "sources.csv")
    cols = ["changed_utc", "sensor", "creek", "kind", "unit", "source", "source_id", "label", "number",
            "thresholds_hash", "storm_sizes_hash", "change"]
    last = {}
    if os.path.exists(p):
        import csv
        for r in csv.DictReader(open(p)):
            last[r["sensor"]] = r
    out = []
    for k, m in sorted(meta.items()):
        row = {"changed_utc": retrieved, "sensor": k, "creek": m.get("creek", ""), "kind": m.get("kind", ""),
               "unit": m.get("unit", ""), "source": m.get("source", ""), "source_id": m.get("source_id", ""),
               "label": m.get("label", ""), "number": m.get("number", ""),
               "thresholds_hash": short_hash([m.get("thresholds"), m.get("rain_triggers")]),
               "storm_sizes_hash": short_hash(m.get("freq"))}
        prev = last.get(k)
        diff = [c for c in ("creek", "kind", "unit", "source", "source_id", "label", "number", "thresholds_hash",
                            "storm_sizes_hash") if prev is None or prev.get(c, "") != str(row[c])]
        if diff:
            row["change"] = "first record" if prev is None else "changed: " + ", ".join(diff)
            out.append(row)
    append_csv(p, cols, out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=2, help="complete days back to save, plus today")
    ap.add_argument("--no-tides", action="store_true")
    a = ap.parse_args()

    retrieved = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")   # to the second: orders versions
    today = dt.datetime.now(PT).date()
    days = [today - dt.timedelta(days=i) for i in range(a.days, -1, -1)]
    start_utc = dt.datetime.combine(days[0], dt.time(0), PT).astimezone(UTC)

    problems, rows, meta, generated = [], [], {}, ""
    try:
        txt = http_get(CREEKS_URL)
        cj, cj_exact = json.loads(txt), json.loads(txt, parse_float=str, parse_int=str)
        generated = cj.get("generated", "")
        rows, meta = creek_rows(cj_exact, cj, retrieved)
    except Exception as e:
        problems.append(f"creeks.json: {e}")
    if not a.no_tides:
        trows, tp = tide_rows(start_utc, now_utc(), retrieved)
        rows += trows
        problems += tp

    by_day = {}
    for r in rows:
        d = dt.date.fromisoformat(r["datetime_local"][:10])
        if d in days:
            by_day.setdefault(d, []).append({k: "" if v is None else str(v) for k, v in r.items()})

    added_total = 0
    for d in days:
        old = read_raw(d)
        merged, added = merge_versions(old, by_day.get(d, []))
        if not merged:
            continue
        if added:
            write_raw(d, merged, previous=old)
        had = {(o["sensor"], o["datetime_utc"]) for o in old}
        revised = sum(1 for r in added if (r["sensor"], r["datetime_utc"]) in had)
        added_total += len(added)
        print(f"{d}: {len(merged):6d} rows, {len(added)} added ({revised} revised values)")

    if meta:
        os.makedirs(os.path.join(ROOT, "meta"), exist_ok=True)
        with open(os.path.join(ROOT, "meta", "sensors.json"), "w") as f:
            json.dump({"updated": generated, "sensors": meta}, f, indent=1, sort_keys=True)
        changes = log_source_changes(meta, retrieved)
        if changes:
            print(f"meta/sources.csv: {len(changes)} sensor(s) new or changed")

    stale = ""
    if generated:
        g = dt.datetime.strptime(generated[:16], "%Y-%m-%d %H:%M").replace(tzinfo=PT)
        age = (dt.datetime.now(PT) - g).total_seconds() / 3600
        if age > STALE_HOURS:
            stale = f"live data is {age:.0f} h old (generated {generated})"
    append_csv(os.path.join(ROOT, "meta", "runs.csv"),
               ["run_utc", "code_version", "creeks_generated", "readings_fetched", "rows_added", "problems"],
               [{"run_utc": retrieved, "code_version": code_version(), "creeks_generated": generated,
                 "readings_fetched": len(rows), "rows_added": added_total,
                 "problems": " | ".join(problems + ([stale] if stale else []))}])
    for p in problems:
        print(f"::warning::{p}", file=sys.stderr)
    if stale or not rows:
        print(f"::error::{stale or 'nothing could be read'}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
