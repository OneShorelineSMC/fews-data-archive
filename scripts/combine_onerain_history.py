"""
Combine the pieces of an OneRain history pull (one folder per water year, from the "OneRain history (all creeks)"
workflow) into one file per gauge for the whole period, plus one report.

    python scripts/combine_onerain_history.py <folder with one subfolder per piece> <output folder>

Readings are de-duplicated on (time to the second, value), so overlapping pieces don't double-count; two rain tips
in the same minute have different seconds and are both kept. Sorted by UTC time. If two pieces give different
values for the same second, both are kept and listed in the report (it shouldn't happen).
"""

import csv
import datetime as dt
import os
import re
import sys
from collections import defaultdict


def main():
    src, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    by_gauge, reports, cols, seen = defaultdict(dict), [], None, set()
    for piece in sorted(os.listdir(src)):
        d = os.path.join(src, piece)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            p = os.path.join(d, fn)
            if fn == "report.md":
                reports.append((piece, open(p).read()))
            elif fn.endswith(".csv"):
                seen.add(fn[:-4])
                with open(p, newline="") as f:
                    r = csv.DictReader(f)
                    if r.fieldnames and len(r.fieldnames) > len(cols or []):
                        cols = r.fieldnames   # a gauge with no readings in a piece has a header-only file
                    for row in r:
                        if row.get("datetime_utc"):
                            by_gauge[fn[:-4]].setdefault((row["datetime_utc"], row["value"]), row)

    summary, clashes = [], []
    for g, rows in sorted(by_gauge.items()):
        data = sorted(rows.values(), key=lambda r: r["datetime_utc"])
        times = defaultdict(set)
        for r in data:
            times[r["datetime_utc"]].add(r["value"])
        clashes += [f"{g} {t}: {sorted(v)}" for t, v in times.items() if len(v) > 1]
        with open(os.path.join(out, f"{g}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
            w.writeheader()
            w.writerows(data)
        per_wy = defaultdict(int)
        for r in data:
            t = dt.datetime.strptime(r["datetime_utc"][:10], "%Y-%m-%d")
            per_wy[t.year + 1 if t.month >= 10 else t.year] += 1
        summary.append((g, len(data), data[0]["datetime_local"] if data else "–",
                        data[-1]["datetime_local"] if data else "–", dict(per_wy)))

    incomplete = [f"{piece}: {line.strip()}" for piece, txt in reports for line in txt.splitlines() if "INCOMPLETE" in line]
    years = sorted({y for *_, w in summary for y in w})
    with open(os.path.join(out, "report.md"), "w") as f:
        f.write("# OneRain history, all creeks\n\n")
        f.write(f"Combined from {len(reports)} pieces: {', '.join(p for p, _ in reports)}.\n\n")
        f.write("## Readings by gauge and water year\n\n| Gauge | Readings | First | Last | "
                + " | ".join(f"WY{y}" for y in years) + " |\n|---|---|---|---|" + "---|" * len(years) + "\n")
        for g, n, a, b, w in summary:
            f.write(f"| {g} | {n} | {a} | {b} | " + " | ".join(str(w.get(y, 0)) for y in years) + " |\n")
        empty = sorted(seen - set(by_gauge))
        if empty:
            f.write("\nNo readings at all in this period: " + ", ".join(empty) + "\n")
        f.write("\n## Problems\n\n")
        f.write(("Pieces that could not be read completely:\n\n" + "\n".join(f"- {x}" for x in incomplete) + "\n\n")
                if incomplete else "No incomplete pieces.\n\n")
        f.write(("Same second, different values (both kept):\n\n" + "\n".join(f"- {x}" for x in clashes[:200]) + "\n\n")
                if clashes else "No conflicting readings.\n\n")
        for piece, txt in reports:
            m = re.search(r"## How far back.*?(?=\n## |\Z)", txt, flags=re.S)
            if m:
                f.write(m.group(0).replace("## How far back", f"## How far back ({piece})") + "\n")
        f.write("\n## Piece reports\n\n")
        for piece, txt in reports:
            f.write(f"<details><summary>{piece}</summary>\n\n{txt}\n</details>\n\n")
    print(open(os.path.join(out, "report.md")).read().split("## Piece reports")[0])


if __name__ == "__main__":
    main()
