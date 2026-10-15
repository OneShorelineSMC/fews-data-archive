"""
Status log: every time a creek's status changes on the live site, add one row to

    status/YYYY/YYYY-MM.csv

with what the site showed and why. Run every 15 minutes; it commits only when something changed.
A change is a different status, flood level, rain trigger, rain level, or "rising fast" starting or
stopping (ordinary rising/steady/falling flips are not logged, to keep the log readable).

The last state seen is kept in status/last.json.
"""

import csv
import json
import os

from common import ROOT, code_version, load_creeks

COLS = ["datetime_local", "creek", "preview", "status", "level", "level_name", "trigger", "rain_level",
        "rain_driver", "rising_fast", "trend_ft_per_hr", "tide", "reasons", "staff_notes", "changed", "code_version"]


def snapshot(c):
    s = c.get("status") or {}
    rain = s.get("rain") or {}
    trend = s.get("trend") or {}
    trig = s.get("trigger")
    return {
        "creek": c["id"], "preview": "yes" if c.get("preview_only") else "no",
        "status": s.get("key") or "", "level": s.get("level") if s.get("level") is not None else "",
        "level_name": s.get("level_name") or "",
        "trigger": (trig.get("name") if isinstance(trig, dict) else trig) or "",
        "rain_level": rain.get("level") if rain.get("level") is not None else "",
        "rain_driver": json.dumps(rain.get("driver"), separators=(",", ":")) if rain.get("driver") else "",
        "rising_fast": "yes" if trend.get("trend") in ("fast", "rising_fast") else "no",
        "trend_ft_per_hr": trend.get("ft_per_hr") if trend.get("ft_per_hr") is not None else "",
        "tide": json.dumps(s.get("tide"), separators=(",", ":")) if s.get("tide") else "",
        "reasons": "; ".join(map(str, s.get("reasons") or [])),
        "staff_notes": "; ".join(map(str, s.get("staff_notes") or [])),
    }


WATCH = ("status", "level", "trigger", "rain_level", "rising_fast")


def main():
    cj = load_creeks()
    when = cj.get("generated", "")[:16]
    state_p = os.path.join(ROOT, "status", "last.json")
    last = json.load(open(state_p)) if os.path.exists(state_p) else {}
    out = []
    now = {}
    for c in cj.get("creeks", []) + cj.get("creeks_preview", []):
        snap = snapshot(c)
        now[c["id"]] = {k: snap[k] for k in WATCH}   # only watched fields, so the file changes only on a real change
        prev = last.get(c["id"])
        diff = [k for k in WATCH if prev is None or str(prev.get(k)) != str(snap.get(k))]
        if diff:
            out.append(dict(snap, datetime_local=when, code_version=code_version(),
                            changed="first record" if prev is None else ", ".join(diff)))
    if out:
        p = os.path.join(ROOT, "status", when[:4], f"{when[:7]}.csv")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        new = not os.path.exists(p)
        with open(p, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n")
            if new:
                w.writeheader()
            w.writerows(out)
        for r in out:
            print(f"{when} {r['creek']}: {r['status']} (level {r['level']}, rain {r['rain_level']}) — {r['changed']}")
    else:
        print(f"{when}: no status changes")
    os.makedirs(os.path.dirname(state_p), exist_ok=True)
    with open(state_p, "w") as f:
        json.dump(now, f, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
