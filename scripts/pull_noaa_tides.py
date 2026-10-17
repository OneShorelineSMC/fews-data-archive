"""
Pull NOAA tide history for a station and date range: the measured water level (hourly) and the tide table
(predicted, hourly), both ft NAVD88, times in UTC and Pacific. Writes one CSV per station plus report.md.
Used for storm checks such as "does Colma Creek back up at high tide?". Nothing is committed to the repo.

    python scripts/pull_noaa_tides.py --stations 9414750 --start 2021-10-01 --end 2022-03-31 --out tides
"""
import argparse
import csv
import datetime as dt
import os
import sys
import urllib.parse
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import NOAA, http_get

PT = ZoneInfo("America/Los_Angeles")
UTC = dt.timezone.utc


def chunks(a, b, days=31):
    t = a
    while t <= b:
        e = min(b, t + dt.timedelta(days=days - 1))
        yield t, e
        t = e + dt.timedelta(days=1)


def get(station, product, a, b):
    q = dict(product=product, station=station, begin_date=a.strftime("%Y%m%d"), end_date=b.strftime("%Y%m%d"),
             datum="NAVD", units="english", time_zone="gmt", format="csv", application="OneShoreline_FEWS")
    if product == "predictions":
        q["interval"] = "h"
    txt = http_get(NOAA + "?" + urllib.parse.urlencode(q))
    rows = list(csv.reader(txt.splitlines()))
    if not rows or "Error" in txt[:200]:
        return {}, txt.strip()[:200]
    out = {}
    for r in rows[1:]:
        if len(r) >= 2 and r[1].strip():
            out[r[0].strip()] = r[1].strip()
    return out, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stations", default="9414750")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", default="tides")
    a = ap.parse_args()
    s, e = dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end)
    os.makedirs(a.out, exist_ok=True)
    rep = [f"# NOAA tide history {s} to {e}", ""]
    for st in [x.strip() for x in a.stations.split(",") if x.strip()]:
        obs, pred, errs = {}, {}, []
        for c0, c1 in chunks(s, e):
            o, err = get(st, "hourly_height", c0, c1)
            if not o:      # hourly_height exists only for verified data; recent months come as 6-minute water_level
                o, err = get(st, "water_level", c0, c1)
                o = {k: v for k, v in o.items() if k.endswith(":00")}
            if err: errs.append(f"{c0}–{c1} measured: {err}")
            obs.update(o)
            p, err = get(st, "predictions", c0, c1)
            if err: errs.append(f"{c0}–{c1} predicted: {err}")
            pred.update(p)
        path = os.path.join(a.out, f"tide_{st}.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["datetime_utc", "datetime_local", "measured_ft_navd88", "predicted_ft_navd88", "surge_ft"])
            for k in sorted(set(obs) | set(pred)):
                t = dt.datetime.strptime(k, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
                m, p = obs.get(k, ""), pred.get(k, "")
                surge = f"{float(m) - float(p):.3f}" if m and p else ""
                w.writerow([t.strftime("%Y-%m-%dT%H:%MZ"), t.astimezone(PT).strftime("%Y-%m-%d %H:%M"), m, p, surge])
        rep += [f"## {st}", f"- hours with a measured level: {len(obs)}; with a predicted level: {len(pred)}"]
        rep += [f"- problem: {x}" for x in errs] + [""]
    open(os.path.join(a.out, "report.md"), "w").write("\n".join(rep) + "\n")
    print("\n".join(rep))


if __name__ == "__main__":
    main()
