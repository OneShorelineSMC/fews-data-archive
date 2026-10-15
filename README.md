# OneShoreline FEWS data archive

A permanent record of what the Flood Early Warning System measured and showed. The live site
([oneshorelinefews](https://github.com/OneShorelineSMC/oneshorelinefews)) only keeps the last 48 hours. This repo
keeps everything, flags events, and can stitch any period back together.

It only **reads** from the live site (its published `creeks.json`) and from NOAA. Nothing here can affect the
website, the kiosk or the data job.

> **Data status.** Everything here is **provisional**, as the agencies published it at the time, and has **not
> been reviewed or approved by OneShoreline staff**. Agencies can revise provisional values later. For official
> records, use the source agency: USGS, NOAA CO-OPS, or OneRain/OneShoreline.

## What's in it

| Path | What | Updated |
|---|---|---|
| `raw/YYYY/MM/YYYY-MM-DD.csv.gz` | Every reading for one Pacific day: creek stage, rain, and NOAA tides at Redwood City and Alameda (measured and predicted, 6-min). Append-only (see the integrity rules). | twice daily |
| `events/events.csv` | One row per event: rain storms (with storm size from NOAA Atlas 14), rain triggers, creek flood levels reached, rapid rises, and high tides (predicted, measured, surge). Overlapping events share a `storm_id`. Rebuilt from the whole record on every run. | twice daily |
| `events/issued.csv` | Append-only: each completed event exactly as first recorded, with the rules and code that flagged it. If a later rule change re-flags history, this file still shows what was originally reported. | when an event completes |
| `status/YYYY/YYYY-MM.csv` | Every change in a creek's status on the live site, with what it showed and why. | every 15 min, only on a change |
| `meta/sensors.json` | Current sensor list: creek, kind, units, source, thresholds, rain triggers, Atlas 14 storm sizes. | twice daily |
| `meta/sources.csv` | Append-only: a row whenever a sensor is added, or its source, units, label, thresholds or storm sizes change. | on a change |
| `meta/runs.csv` | One row per archive run: when, code version, when the live data was generated, readings fetched, rows added, problems. | twice daily |
| `scripts/` | The code: Python standard library, plus `openpyxl` for Excel output only. | |

## Integrity rules

1. **Nothing is overwritten.** Raw files are append-only. A revised value (NOAA preliminary → verified, or a
   provisional reading re-published differently) is added as a new row next to the old one, with its own
   `retrieved_utc`. The "current" value is the most recently retrieved one; every earlier version stays. The code
   refuses to write a day file that would lose an archived row.
2. **Values exactly as published.** Numbers are stored as the text the source sent, with no re-rounding or unit
   conversion.
3. **Every reading names its source.** It records who measured it (`source`), which station, sensor or product
   (`source_id`), how we got it (`via`), and when (`retrieved_utc`). The agency's own quality codes
   (`source_qualifier`) are kept unchanged next to a common quality word.
4. **UTC is the time of record.** Pacific clock time is kept for reading. `time_flag` marks clock times that are
   ambiguous (the repeated 1 AM hour when daylight saving ends) or impossible (spring forward).
5. **Every flag is traceable.** Each event row carries `rules_version` (the event rules), `code_version` (git
   commit), `meta_hash` (the thresholds and storm sizes in force), `data_sources` and `built_utc`.
6. **Health check.** A run **fails, and GitHub emails the repo admins**, if the live data is more than 6 hours old
   or nothing could be read. It still saves whatever it did get first. A NOAA outage alone only adds a warning.
   Problems are logged in `meta/runs.csv`.
7. **No gaps from a missed run.** The archive runs twice a day and re-reads the last two days each time, because
   the live site keeps only 48 hours. One failed run loses nothing.

### Raw file columns

| Column | Meaning |
|---|---|
| `sensor` | Permanent key, same as the live site (`bel_stage`). Tides: `tide_<station>_measured` / `_predicted` |
| `creek` | Creek id (blank for tides) |
| `kind` | `stage`, `rain_increment`, `tide_measured`, `tide_predicted` |
| `datetime_utc` | Time of the reading, UTC (`2026-12-01T08:15Z`) |
| `datetime_local` | Pacific clock time (PST/PDT) |
| `time_flag` | blank, `dst_repeated_hour`, or `dst_skipped_hour` |
| `value` | As published |
| `unit` | `ft` (gauge datum), `in`, `ft NAVD88` (tides) |
| `quality` | `provisional` (OneRain, USGS via the live site), `preliminary` / `verified` (NOAA measured), `predicted` |
| `source_qualifier` | The agency's own codes, unchanged. NOAA: `q=` quality, `f=` flags, `s=` sigma |
| `source` | `onerain`, `usgs`, `noaa` |
| `source_id` | Station and sensor: `onerain 41104 site_id=2 device_id=4`, `usgs 11162753 00065`, `noaa 9414523 water_level Redwood City` |
| `via` | How it was retrieved: `fews creeks.json` or `noaa api` |
| `retrieved_utc` | When this archive read it (to the second). Orders versions |

## Getting data out: Stitch data

**Actions → Stitch data → Run workflow.** Enter the dates and what you want: creeks like `belmont,colma`, sensors
like `bel_stage`, `tide_9414523`, or `all`. Then pick a time step; 15 min is a good default. When it finishes,
download the result from the run page:

- `stitched_<start>_<end>.csv` and `.xlsx`: one column per sensor on a common time step. Each sensor has a `_flag`
  column:
  - blank: measured
  - `held`: no new reading, so the last value is carried forward. This is normal for gauges that record hourly or
    only on change.
  - `gap`: silent longer than normal for that gauge
- Step `0` gives every reading as recorded, in one long table with all the raw columns. Tick **all versions** to
  also see every earlier version of revised values.
- `sources_<start>_<end>.csv` (and a `sources` sheet in Excel): for each sensor, who measured it, how it was
  retrieved, the quality codes seen, and how many readings were revised. Cite this sheet whenever the data is
  shared.
- `gaps_<start>_<end>.csv`: every time a sensor went quiet longer than its normal rhythm.

A creek's tide station is included automatically.

## Event rules (top of `scripts/find_events.py`, `RULES_VERSION`)

| Event | Rule |
|---|---|
| rain | Starts at the first tip and ends after 6 dry hours; kept if ≥ 0.10 in. Records the peak depth for each duration from 15 min to 48 hr. The most extreme of those sets the storm size: its return period and its chance in any year (Atlas 14 at that gauge), as level 1–3 (Heavy / Very heavy / Severe). |
| rain_trigger | A rain trigger in the live site's `thresholds.csv` was met (Belmont 30-min; 4-hr + tide) |
| creek_level | Stage at or above the gauge's lowest flood level: peak, time, highest level reached, hours above |
| rising_fast | Rose ≥ 0.5 ft within 60 min (the site's provisional rule) |
| tide | A high tide above MHHW, or a measured tide ≥ 0.5 ft above predicted: predicted, measured, surge, height above MHHW |

When a rule changes, bump `RULES_VERSION`. `events.csv` re-flags the whole record under the new rules, while
`issued.csv` keeps what was originally flagged. `storm_id` comes from the storm's start time
(`S20261201T0815Z`), so it doesn't change when older data is added.

## Adding older data (backfill)

Older records can be added in the same daily-file format: USGS history (the **USGS history pull** in the live
repo), NOAA tide history, and OneRain exports. Once added, they stitch seamlessly with the live archive. Backfilled
rows get their own `via` (for example `usgs history pull`) and `retrieved_utc`, so they are never confused with
what the live site showed. Ask for an import script when you have the files.

## Size and limits

About 8 KB per day now, roughly 3–7 MB a year with tides. GitHub recommends repos stay under 1 GB, and limits
single files to 100 MB. Daily files stop changing once NOAA verifies its tides, so git history stays small.

**Keep this repo public.** The status log runs about 2,900 times a month. Actions minutes are free and unlimited
for public repos; private repos on the free plan get 2,000 minutes a month. Everything here is already public
data. GitHub turns off scheduled workflows in public repos after 60 days without activity. The archive commits keep
the repo active, but if you get an email saying a workflow was disabled, re-enable it on the Actions tab.

## Setup (once)

1. Create the repo `OneShorelineSMC/fews-data-archive` as **Public** and upload these files, keeping the folders.
2. **Settings → Actions → General → Workflow permissions → Read and write permissions → Save.**
3. **Actions** tab → enable workflows if asked → run **Daily archive and events** once, then **Status log** once.
4. Make sure repo admins have GitHub email notifications on for failed workflow runs (the health check).

## Notes

- Gauges that record only on change look sparse in quiet weather; that is normal.
- Stage is in feet above each gauge's own datum, not NAVD88, unless the gauge's notes say otherwise.
