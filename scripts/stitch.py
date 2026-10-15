"""
Stitch the daily archive (and any imported history) into one continuous record.

    python scripts/stitch.py --start 2026-12-01 --end 2027-03-31 --select belmont
    python scripts/stitch.py --start 2026-12-01 --end 2026-12-31 --select bel_stage,bel_rain,tide_9414523 --step 15 --excel

--select   creek ids (belmont), sensor keys (bel_stage), 'tide_<station>' (both measured and predicted),
           or 'all'. Comma-separated.
--step     resample to a common time step in minutes (stage and tide: average; rain: sum).
           Without it you get every reading as recorded, in a long table.
--excel    also write an .xlsx with one sheet per creek (needs openpyxl).
--versions current (default): the latest retrieved version of each reading.
           all: every version ever archived (to see what was revised, e.g. NOAA preliminary -> verified).
           Only with --step 0.

Output in out/: stitched_<start>_<end>.csv (+ .xlsx), sources_<start>_<end>.csv (who measured each sensor and
how it was retrieved), and gaps_<start>_<end>.csv listing every time a
sensor went quiet for longer than its normal reporting rhythm. With --step, empty bins are left blank
and flagged in a <sensor>_flag column: 'held' = no new reading, last value carried forward
(normal for gauges that record hourly or only on change); 'gap' = silent longer than normal.
"""

import argparse
import csv
import datetime as dt
import os
import statistics

from common import PT, RAW_COLS, ROOT, UTC, code_version, current, fmt_local, parse_utc, read_raw_range, sensor_meta


def pick(rows, select, meta):
    want = {s.strip() for s in select.split(",") if s.strip()}
    if "all" in want:
        return rows
    keys = set()
    for w in want:
        if w.startswith("tide_"):
            keys |= {r["sensor"] for r in rows if r["sensor"].startswith(w)}
        else:
            keys |= {k for k, m in meta.items() if m.get("creek") == w}
            keys |= {r["sensor"] for r in rows if r["creek"] == w}
            keys.add(w)
    # a creek's tide station comes along with the creek
    for w in want:
        st = next((m.get("tide_station") for m in meta.values() if m.get("creek") == w and m.get("tide_station")), None)
        if st:
            keys |= {r["sensor"] for r in rows if r["sensor"].startswith(f"tide_{st}")}
    return [r for r in rows if r["sensor"] in keys]


def limits(rows, meta):
    """How long a silence is normal for each sensor, in seconds: 3x its typical interval, at least 1 h;
    13 h for gauges that record only on change; 25 h for rain gauges (they record only when it rains)."""
    by, out = {}, {}
    for r in rows:
        by.setdefault(r["sensor"], []).append(parse_utc(r["datetime_utc"]))
    for k, ts in by.items():
        ts.sort()
        typical = statistics.median((b - a).total_seconds() for a, b in zip(ts, ts[1:])) if len(ts) > 2 else 3600
        m = meta.get(k, {})
        floor = 25 * 3600 if m.get("kind") == "rain_increment" or k.endswith("rain") else (
            13 * 3600 if m.get("reports") == "on change" else 3600)
        out[k] = (sorted(ts), max(3 * typical, floor))
    return out


def gaps(rows, meta):
    """Silences longer than normal for each sensor."""
    out = []
    for k, (ts, limit) in limits(rows, meta).items():
        if len(ts) < 2:
            continue
        for a, b in zip(ts, ts[1:]):
            if (b - a).total_seconds() > limit:
                out.append({"sensor": k, "gap_start_local": fmt_local(a), "gap_end_local": fmt_local(b),
                            "hours": round((b - a).total_seconds() / 3600, 1)})
    return out


def resample(rows, start, end, step, lim):
    sensors = sorted({r["sensor"] for r in rows})
    kind = {r["sensor"]: r["kind"] for r in rows}
    bins = {}
    for r in rows:
        t = parse_utc(r["datetime_utc"])
        b = t - dt.timedelta(minutes=t.minute % step, seconds=t.second)
        bins.setdefault((r["sensor"], b), []).append(float(r["value"]))
    last = {}
    for (s, b), v in sorted(bins.items(), key=lambda x: x[0][1]):   # seed from readings before the window
        if b < start and kind[s] != "rain_increment":
            last[s] = (b, round(sum(v) / len(v), 3))
    out, t = [], start
    while t < end:
        row = {"datetime_local": fmt_local(t), "datetime_utc": t.strftime("%Y-%m-%dT%H:%MZ")}
        for s in sensors:
            v = bins.get((s, t))
            if v is not None:
                row[s] = round(sum(v), 3) if kind[s] == "rain_increment" else round(sum(v) / len(v), 3)
                row[s + "_flag"] = ""
                last[s] = (t, row[s])
            elif kind[s] == "rain_increment":
                # rain gauges log tips only: an empty bin means no rain, unless the gauge is silent too long
                lt = last.get(s, (None,))[0]
                row[s] = 0.0
                row[s + "_flag"] = "" if lt is None or (t - lt).total_seconds() <= lim.get(s, (0, 9e9))[1] else "gap?"
            else:
                lt, lv = last.get(s, (None, None))
                if lt is not None and (t - lt).total_seconds() <= lim.get(s, (0, 3600))[1]:
                    row[s], row[s + "_flag"] = lv, "held"     # no new reading; level assumed unchanged
                else:
                    row[s], row[s + "_flag"] = "", "gap"
        out.append(row)
        t += dt.timedelta(minutes=step)
    cols = ["datetime_local", "datetime_utc"] + [c for s in sensors for c in (s, s + "_flag")]
    return out, cols


SRC_COLS = ["sensor", "creek", "kind", "unit", "source", "source_id", "via", "qualities", "readings",
            "first_utc", "last_utc", "versions_revised"]


def sources(rows, t0, t1):
    """One row per sensor and source: who measured it, how it was retrieved, what quality codes it carried."""
    out = {}
    for r in rows:
        t = parse_utc(r["datetime_utc"])
        if not (t0 <= t < t1):
            continue
        k = (r["sensor"], r.get("source_id", ""), r.get("via", ""))
        o = out.setdefault(k, {"sensor": r["sensor"], "creek": r["creek"], "kind": r["kind"], "unit": r["unit"],
                               "source": r["source"], "source_id": k[1], "via": k[2], "q": set(), "times": {},
                               "first_utc": r["datetime_utc"], "last_utc": r["datetime_utc"]})
        o["q"].add(r.get("quality", ""))
        o["times"][r["datetime_utc"]] = o["times"].get(r["datetime_utc"], 0) + 1
        o["first_utc"] = min(o["first_utc"], r["datetime_utc"])
        o["last_utc"] = max(o["last_utc"], r["datetime_utc"])
    res = []
    for o in out.values():
        o["qualities"] = ", ".join(sorted(q for q in o.pop("q") if q))
        times = o.pop("times")
        o["readings"] = len(times)
        o["versions_revised"] = sum(1 for n in times.values() if n > 1)
        res.append(o)
    return sorted(res, key=lambda o: (o["sensor"], o["first_utc"]))


def write_csv(path, cols, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (Pacific)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (Pacific), inclusive")
    ap.add_argument("--select", default="all")
    ap.add_argument("--step", type=int, default=0, help="minutes; 0 = every reading")
    ap.add_argument("--excel", action="store_true")
    ap.add_argument("--versions", choices=["current", "all"], default="current")
    a = ap.parse_args()
    if a.versions == "all" and a.step:
        ap.error("--versions all needs --step 0 (every reading)")

    d0, d1 = dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end)
    meta = sensor_meta().get("sensors", {})
    every = pick(read_raw_range(d0 - dt.timedelta(days=1), d1 + dt.timedelta(days=1), versions="all"), a.select, meta)
    rows = every if a.versions == "all" else current(every)
    t0 = dt.datetime.combine(d0, dt.time(0), PT).astimezone(UTC)
    t1 = dt.datetime.combine(d1 + dt.timedelta(days=1), dt.time(0), PT).astimezone(UTC)
    padded = [r for r in rows if parse_utc(r["datetime_utc"]) < t1]   # includes the day before, to carry levels in
    rows = [r for r in padded if parse_utc(r["datetime_utc"]) >= t0]
    os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
    tag = f"{d0}_{d1}"

    best = current(rows)
    g = gaps(best, meta)
    srcs = sources(every, t0, t1)
    write_csv(os.path.join(ROOT, "out", f"sources_{tag}.csv"), SRC_COLS, srcs)
    write_csv(os.path.join(ROOT, "out", f"gaps_{tag}.csv"), ["sensor", "gap_start_local", "gap_end_local", "hours"], g)

    if a.step:
        table, cols = resample(current(padded), t0, t1, a.step, limits(current(padded), meta))
    else:
        cols = RAW_COLS
        table = rows
    p = os.path.join(ROOT, "out", f"stitched_{tag}.csv")
    write_csv(p, cols, table)
    print(f"{len(best)} readings ({len(rows)} rows, versions={a.versions}) from {len({r['sensor'] for r in rows})} "
          f"sensors -> {p} ({len(table)} rows); {len(g)} gaps; {len(srcs)} source(s)")

    if a.excel:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook(); wb.remove(wb.active)
        groups = {}
        for s in sorted({r["sensor"] for r in rows}):
            groups.setdefault(meta.get(s, {}).get("creek") or ("tides" if s.startswith("tide_") else "other"), []).append(s)
        for creek, sens in groups.items():
            ws = wb.create_sheet(creek[:31])
            if a.step:
                cc = ["datetime_local"] + [c for s in sens for c in (s, s + "_flag")]
                data = [[r.get(c, "") for c in cc] for r in table]
            else:
                cc = cols
                data = [[r.get(c, "") for c in cc] for r in table if r["sensor"] in sens]
            ws.append(cc)
            for c in ws[1]:
                c.font = Font(bold=True)
            for row in data:
                ws.append([float(x) if isinstance(x, str) and x.replace(".", "", 1).replace("-", "", 1).isdigit() else x for x in row])
            ws.freeze_panes = "A2"
        ws = wb.create_sheet("gaps")
        ws.append(["sensor", "gap_start_local", "gap_end_local", "hours"])
        for r in g:
            ws.append([r["sensor"], r["gap_start_local"], r["gap_end_local"], r["hours"]])
        ws = wb.create_sheet("sources")
        ws.append(SRC_COLS)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in srcs:
            ws.append([r[c] for c in SRC_COLS])
        ws.freeze_panes = "A2"
        ws = wb.create_sheet("about")
        for line in [["Stitched from the OneShoreline FEWS data archive"], ["Dates (Pacific)", a.start, a.end],
                     ["Selection", a.select], ["Time step (min)", a.step or "every reading"],
                     ["Versions", a.versions + (" (every archived version; latest retrieved_utc is current)"
                                                if a.versions == "all" else " (latest retrieved version of each reading)")],
                     ["Times", "UTC (datetime_utc) is authoritative; Pacific clock time (datetime_local) for reading. "
                               "time_flag marks the repeated hour when daylight saving ends"],
                     ["Quality", "OneRain, USGS and NOAA preliminary data are provisional and can be revised; "
                                 "source_qualifier holds the agency's own codes, unchanged; tide heights ft NAVD88"],
                     ["Sources", "See the 'sources' sheet: who measured each sensor and how it was retrieved"],
                     ["Not reviewed", "Data have not been reviewed or approved by OneShoreline staff"],
                     ["Built", dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ"), "code " + code_version()]]:
            ws.append(line)
        xp = p.replace(".csv", ".xlsx")
        wb.save(xp)
        print(f"-> {xp}")


if __name__ == "__main__":
    main()
