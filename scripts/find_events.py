"""
Event log: scan the whole archive and write events/events.csv, one row per event:

  rain          a rain gauge's storm (starts at the first tip, ends after RAIN_DRY_HOURS dry; kept if
                the total is at least RAIN_MIN_IN). Peak depth for each duration (15 min - 48 hr), the
                most extreme one, its storm size (return period, chance in any year) from NOAA Atlas 14.
  rain_trigger  a rain trigger from thresholds.csv was met (e.g. Belmont 30-min rain; 4-hr rain + tide).
  creek_level   a stage gauge at or above its lowest flood level: peak stage and time, highest level
                reached, hours above.
  rising_fast   a stage gauge rose at least RISE_FT within RISE_MINUTES (the site's rule).
  tide          a high tide that was above MHHW (measured or predicted) or ran at least TIDE_RESID_FT
                above the prediction: predicted, measured and the difference (surge).

Events that overlap in time (within GROUP_HOURS) share a storm_id (S<start time UTC>), so you can ask e.g.
"every storm with 2-year rain and a tide above MHHW".

Two files, on purpose:
  events/events.csv   rebuilt from the whole archive every run with the current rules, thresholds and storm
                      sizes (so a better rule re-flags the full record)
  events/issued.csv   append-only: each event as it was first flagged once complete, never rewritten. This is
                      the record of what the archive said at the time.
Every row carries data_sources (which sensors/products), rules_version (this file's rules), code_version (git
commit) and meta_hash (the thresholds and storm-size tables used), so any flag can be traced and reproduced.
"""

import bisect
import csv
import datetime as dt
import math
import os

from common import (ROOT, UTC, append_csv, code_version, fmt_local, parse_utc, raw_days, read_raw_range,
                    sensor_meta, short_hash)

RULES_VERSION = "2026-10-15.1"   # bump when any rule or constant below changes

RAIN_MIN_IN = 0.10       # smallest storm total kept
RAIN_DRY_HOURS = 6       # dry hours that end a storm
RISE_FT, RISE_MINUTES = 0.5, 60   # same as the site's provisional rapidly-rising rule
TIDE_RESID_FT = 0.5      # measured this much above predicted is flagged
GROUP_HOURS = 6          # events this close together share a storm_id
MHHW = {"9414523": 7.02, "9414750": 6.37}   # ft NAVD88 (lookups/tide_stations.csv)
DURS = [15, 30, 60, 120, 180, 360, 720, 1440, 2880]

COLS = ["storm_id", "type", "start_local", "end_local", "creek", "sensor", "station", "summary",
        "total_in", "peak_minutes", "peak_in", "peak_ari_yr", "peak_aep_pct", "rain_level",
        "level", "level_name", "peak_ft", "peak_local", "hours_above",
        "rise_ft", "predicted_ft", "measured_ft", "surge_ft", "above_mhhw_ft"] + [f"max_{d}min_in" for d in DURS] + [
        "data_sources", "rules_version", "code_version", "meta_hash", "built_utc"]

LEVEL_NAMES = {0: "", 1: "Heavy", 2: "Very heavy", 3: "Severe"}


def series(rows):
    out = {}
    for r in rows:
        try:
            out.setdefault(r["sensor"], []).append((parse_utc(r["datetime_utc"]), float(r["value"])))
        except ValueError:
            continue
    return out


def ari_of(depth, table):
    """Return period (yr) for a depth by log-interpolating Atlas 14 depths; None below the 1-year storm."""
    pts = sorted((float(a), float(v)) for a, v in table.items())
    if not pts or depth < pts[0][1]:
        return None
    for (a0, v0), (a1, v1) in zip(pts, pts[1:]):
        if v0 <= depth < v1:
            return round(math.exp(math.log(a0) + (math.log(a1) - math.log(a0)) * (depth - v0) / (v1 - v0)), 1)
    return pts[-1][0]  # at or above the largest tabulated storm


def rolling_max(pts, minutes):
    """Largest total of increments in any window of this many minutes, and when it ended."""
    best, best_t, j, s = 0.0, None, 0, 0.0
    w = dt.timedelta(minutes=minutes)
    for i, (t, v) in enumerate(pts):
        s += v
        while pts[j][0] <= t - w:
            s -= pts[j][1]
            j += 1
        if s > best + 1e-9:
            best, best_t = s, t
    return round(best, 2), best_t


def rain_events(sensor, pts, meta):
    pts = [(t, v) for t, v in pts if v > 0]
    if not pts:
        return []
    groups, cur = [], [pts[0]]
    for p in pts[1:]:
        if p[0] - cur[-1][0] > dt.timedelta(hours=RAIN_DRY_HOURS):
            groups.append(cur); cur = [p]
        else:
            cur.append(p)
    groups.append(cur)
    freq = (meta.get("freq") or {}).get("minutes") or {}
    levels = (meta.get("freq") or {}).get("levels") or {}
    out = []
    for g in groups:
        total = round(sum(v for _, v in g), 2)
        if total < RAIN_MIN_IN:
            continue
        maxes, best = {}, (0, None, None, 0)   # (ari, minutes, depth, level)
        for d in DURS:
            depth, _ = rolling_max(g, d)
            maxes[d] = depth
            ari = ari_of(depth, freq.get(str(d), {})) if freq else None
            lv = sum(1 for x in levels.get(str(d), []) if depth >= x)
            key = (ari or 0, lv)
            if key > (best[0], best[3]) or best[1] is None:
                best = (ari or 0, d, depth, lv)
        ari, d, depth, lv = best
        e = {"type": "rain", "start": g[0][0], "end": g[-1][0], "creek": meta.get("creek", ""), "sensor": sensor,
             "total_in": total, "peak_minutes": d, "peak_in": depth,
             "peak_ari_yr": ari or "", "peak_aep_pct": round(100 * (1 - math.exp(-1 / ari)), 1) if ari else "",
             "rain_level": lv, "level_name": LEVEL_NAMES.get(lv, "")}
        e["summary"] = (f"{total:.2f} in total; peak {d} min {depth:.2f} in"
                        + (f" ≈ {ari:g}-yr storm" if ari else " (below 1-yr storm)"))
        for k, v in maxes.items():
            e[f"max_{k}min_in"] = v
        out.append(e)
    return out


def stepwise_at(pts, t):
    """Value in effect at time t (last reading at or before t)."""
    lo, hi = 0, len(pts) - 1
    if not pts or pts[0][0] > t:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if pts[mid][0] <= t:
            lo = mid
        else:
            hi = mid - 1
    return pts[lo][1]


def creek_events(sensor, pts, meta):
    out = []
    th = sorted(meta.get("thresholds") or [], key=lambda x: x["value"])
    if th:
        lo = th[0]["value"]
        run = []
        for t, v in pts + [(None, None)]:
            if v is not None and v >= lo:
                run.append((t, v))
            elif run:
                peak_t, peak = max(run, key=lambda x: x[1])
                reached = [x for x in th if peak >= x["value"]][-1]
                end = t or run[-1][0]
                out.append({"type": "creek_level", "start": run[0][0], "end": end, "creek": meta.get("creek", ""),
                            "sensor": sensor, "peak_ft": peak, "peak_local": fmt_local(peak_t),
                            "level": reached.get("level"), "level_name": reached.get("name"),
                            "hours_above": round((end - run[0][0]).total_seconds() / 3600, 1),
                            "summary": f"peak {peak:.2f} ft ({reached.get('name')}) at {fmt_local(peak_t)}"})
                run = []
    # rising fast
    run = []
    w = dt.timedelta(minutes=RISE_MINUTES)
    for t, v in pts + [(None, None)]:
        rise = None
        if t is not None:
            before = stepwise_at(pts, t - w)
            rise = None if before is None else v - before
        if rise is not None and rise >= RISE_FT:
            run.append((t, rise, v))
        elif run:
            m = max(run, key=lambda x: x[1])
            out.append({"type": "rising_fast", "start": run[0][0], "end": run[-1][0], "creek": meta.get("creek", ""),
                        "sensor": sensor, "rise_ft": round(m[1], 2), "peak_ft": max(x[2] for x in run),
                        "summary": f"rose {m[1]:.2f} ft in {RISE_MINUTES} min" + (" (tidal gauge)" if meta.get("tidal") else "")})
            run = []
    return out


def trigger_events(sensor, pts, meta, tides):
    out = []
    for trig in meta.get("rain_triggers") or []:
        w = dt.timedelta(minutes=trig["minutes"])
        st = meta.get("tide_station")
        meas = tides.get(f"tide_{st}_measured", [])
        j, s, run = 0, 0.0, []
        for t, v in pts:
            s += v
            while pts[j][0] <= t - w:
                s -= pts[j][1]; j += 1
            ok = s >= trig["value"]
            if ok and trig.get("high_tide"):
                tv = stepwise_at(meas, t)
                ok = tv is not None and tv >= trig.get("tide_min", MHHW.get(st, 99))
            if ok:
                run.append((t, s))
            elif run:
                out.append({"type": "rain_trigger", "start": run[0][0], "end": run[-1][0], "creek": meta.get("creek", ""),
                            "sensor": sensor, "level": trig.get("level"), "level_name": trig.get("name"),
                            "peak_in": round(max(x[1] for x in run), 2), "peak_minutes": trig["minutes"],
                            "summary": f"{trig.get('label') or trig.get('name')}: {max(x[1] for x in run):.2f} in (trigger {trig['value']} in)"})
                run = []
        if run:
            out.append({"type": "rain_trigger", "start": run[0][0], "end": run[-1][0], "creek": meta.get("creek", ""),
                        "sensor": sensor, "level": trig.get("level"), "level_name": trig.get("name"),
                        "peak_in": round(max(x[1] for x in run), 2), "peak_minutes": trig["minutes"],
                        "summary": f"{trig.get('label') or trig.get('name')} (ongoing)"})
    return out


def tide_events(tides):
    out = []
    for st, mhhw in MHHW.items():
        pred = tides.get(f"tide_{st}_predicted", [])
        meas = tides.get(f"tide_{st}_measured", [])
        if not pred:
            continue
        win = dt.timedelta(hours=3)
        mt = [m[0] for m in meas]
        j0 = 0
        for i, (t, v) in enumerate(pred):
            while pred[j0][0] < t - win:
                j0 += 1
            if v < 3 or (i > 0 and pred[i - 1][1] >= v):    # rising into the peak only (no double count)
                continue
            neigh = [p for p in pred[j0:i + 31] if abs((p[0] - t).total_seconds()) <= win.total_seconds()]
            if v < max(p[1] for p in neigh):                # not the high tide
                continue
            a, b = bisect.bisect_left(mt, t - dt.timedelta(hours=1)), bisect.bisect_right(mt, t + dt.timedelta(hours=1))
            near = meas[a:b]
            mmax = max(near, key=lambda x: x[1]) if near else None
            surge = round(mmax[1] - v, 2) if mmax else None
            top = max(v, mmax[1] if mmax else -99)
            if top > mhhw or (surge is not None and surge >= TIDE_RESID_FT):
                out.append({"type": "tide", "start": t, "end": t, "station": st, "predicted_ft": round(v, 2),
                            "measured_ft": round(mmax[1], 2) if mmax else "", "surge_ft": surge if surge is not None else "",
                            "above_mhhw_ft": round(top - mhhw, 2), "peak_local": fmt_local(mmax[0] if mmax else t),
                            "summary": f"high tide {top:.2f} ft NAVD88 ({top - mhhw:+.2f} vs MHHW)"
                                       + (f", {surge:+.2f} ft vs predicted" if surge is not None else ", measured not available")})
    return out


def main():
    days = raw_days()
    if not days:
        print("archive is empty"); return
    rows = read_raw_range(days[0], days[-1])
    meta = sensor_meta().get("sensors", {})
    S = series(rows)
    tides = {k: v for k, v in S.items() if k.startswith("tide_")}
    events = []
    for k, pts in S.items():
        m = meta.get(k, {})
        kind = m.get("kind") or next((r["kind"] for r in rows if r["sensor"] == k), "")
        if kind == "rain_increment":
            events += rain_events(k, pts, m)
            events += trigger_events(k, pts, m, tides)
        elif kind == "stage":
            events += creek_events(k, pts, m)
    events += tide_events(tides)

    # storm_id: chain events that overlap within GROUP_HOURS
    events.sort(key=lambda e: e["start"])
    gap = dt.timedelta(hours=GROUP_HOURS)
    sid, end = None, None
    for e in events:
        if end is None or e["start"] > end + gap:
            sid = f"S{e['start'].astimezone(UTC):%Y%m%dT%H%MZ}"   # stable: doesn't change when history is added
            end = e["end"]
        e["storm_id"] = sid
        end = max(end, e["end"])

    src = {}
    for r in rows:
        src.setdefault(r["sensor"], set()).add(r.get("source_id") or r.get("source") or "")
    built = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    cv = code_version()
    for e in events:
        keys = [e["sensor"]] if e.get("sensor") else [f"tide_{e['station']}_measured", f"tide_{e['station']}_predicted"]
        if e["type"] == "rain_trigger" and any(t.get("high_tide") for t in meta.get(e["sensor"], {}).get("rain_triggers", [])):
            st = meta.get(e["sensor"], {}).get("tide_station")
            keys.append(f"tide_{st}_measured")
        e["data_sources"] = "; ".join(sorted({s for k in keys for s in src.get(k, set()) if s}))
        m = meta.get(e.get("sensor", ""), {})
        e["meta_hash"] = short_hash([m.get("thresholds"), m.get("rain_triggers"), m.get("freq"), MHHW]) if m else short_hash(MHHW)
        e.update(rules_version=RULES_VERSION, code_version=cv, built_utc=built)
    os.makedirs(os.path.join(ROOT, "events"), exist_ok=True)
    with open(os.path.join(ROOT, "events", "events.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for e in events:
            r = dict(e, start_local=fmt_local(e["start"]), end_local=fmt_local(e["end"]))
            w.writerow({c: r.get(c, "") for c in COLS})
    # issued.csv: complete events only (nothing new for GROUP_HOURS after them), each once, never rewritten
    latest = max(parse_utc(r["datetime_utc"]) for r in rows)
    ip = os.path.join(ROOT, "events", "issued.csv")
    have = set()
    if os.path.exists(ip):
        have = {(r["type"], r["sensor"], r["station"], r["start_local"]) for r in csv.DictReader(open(ip))}
    new = []
    for e in events:
        r = dict(e, start_local=fmt_local(e["start"]), end_local=fmt_local(e["end"]))
        k = (r["type"], r.get("sensor", ""), r.get("station", ""), r["start_local"])
        if k not in have and e["end"] + gap < latest:
            new.append({c: r.get(c, "") for c in COLS})
            have.add(k)
    append_csv(ip, COLS, new)
    if new:
        print(f"events/issued.csv: {len(new)} newly completed event(s) recorded")
    kinds = {}
    for e in events:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    print(f"{len(events)} events from {days[0]} to {days[-1]}: " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())))


if __name__ == "__main__":
    main()
