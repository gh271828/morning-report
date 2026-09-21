#!/usr/bin/env python3
"""
Morning Report -- site builder
==============================

Fetches the data once and writes both outputs from it:

    site/index.html          the page
    site/morning-report.pdf  the printable sheet
    site/morning-report.txt  plain text, for anyone who wants it

    $ python3 build_site.py
    $ python3 build_site.py --out site

Fetching once matters: building the page and the PDF with separate runs
would hit the sources twice and the two could disagree by a forecast cycle.

Requires morning_report.py and morning_report_pdf.py alongside it.
"""

from __future__ import annotations

import argparse
import html
import os
import sys
from datetime import datetime

try:
    import morning_report as mr
    import morning_report_pdf as mrp
except ImportError as e:
    sys.exit(f"Needs morning_report.py and morning_report_pdf.py alongside it ({e}).")


# ---------------------------------------------------------------------------
# Design tokens
#
# Ground: the alkali flat at Badwater, a cool off-white rather than a warm
# cream. Ink: near-black with a trace of the Panamints' blue-violet at dusk.
# Two accents, both semantic rather than decorative -- oxidised iron for
# closures, a dry sage for what is open.
# ---------------------------------------------------------------------------

CSS = """
:root {
  --paper:  #EFEFE9;
  --ink:    #17181A;
  --muted:  #6A6C68;
  --rule:   #C9C9BF;
  --closed:  #8C3A22;
  --caution: #8A6316;
  --open:    #4A6150;
  --leader: #BDBDB2;
}

@media (prefers-color-scheme: dark) {
  :root {
    --paper:  #16171A;
    --ink:    #E9E8E2;
    --muted:  #93958E;
    --rule:   #34363A;
    --closed:  #D08063;
    --caution: #C9A250;
    --open:    #8FAE96;
    --leader: #3C3E42;
  }
}

* { box-sizing: border-box; }

html { -webkit-text-size-adjust: 100%; }

body {
  margin: 0;
  padding: 2.5rem 1.25rem 4rem;
  background: var(--paper);
  color: var(--ink);
  font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Segoe UI",
               sans-serif;
  font-size: 16px;
  line-height: 1.5;
}

.sheet { max-width: 44rem; margin: 0 auto; }

/* ---- masthead ---------------------------------------------------------- */

header { text-align: center; margin-bottom: 2.25rem; }

h1 {
  font-family: Newsreader, Georgia, "Times New Roman", serif;
  font-weight: 500;
  font-size: clamp(1.75rem, 6vw, 2.6rem);
  line-height: 1.1;
  letter-spacing: -0.015em;
  margin: 0;
}

.report-date {
  font-family: Newsreader, Georgia, serif;
  font-size: clamp(1rem, 3vw, 1.2rem);
  font-style: italic;
  color: var(--muted);
  margin: 0.4rem 0 0;
}

.provenance a { color: inherit; text-underline-offset: 2px; }
.provenance a:hover { color: var(--ink); }

.provenance {
  margin: 1.1rem auto 0;
  padding-top: 0.9rem;
  border-top: 1px solid var(--rule);
  max-width: 30rem;
  font-size: 0.8125rem;
  color: var(--muted);
}

/* ---- lede: the nearest forecast period, set to be read ----------------- */

.lede { margin: 0 0 1.75rem; }

.lede .period {
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--muted);
  margin: 0 0 0.35rem;
}

.lede p {
  font-family: Newsreader, Georgia, serif;
  font-size: clamp(1.15rem, 3.4vw, 1.45rem);
  line-height: 1.45;
  margin: 0;
  text-wrap: pretty;
}

.sun {
  margin: 0 0 2.25rem;
  padding: 0.6rem 0;
  border-top: 1px solid var(--rule);
  border-bottom: 1px solid var(--rule);
  font-size: 0.9375rem;
  display: flex;
  gap: 1.5rem;
  flex-wrap: wrap;
}
.sun span { color: var(--muted); }
.sun b { font-weight: 600; color: var(--ink); }

/* ---- sections ---------------------------------------------------------- */

section { margin-bottom: 2rem; }

h2 {
  font-family: Newsreader, Georgia, serif;
  font-weight: 500;
  font-size: 1.25rem;
  margin: 0 0 0.75rem;
  padding-bottom: 0.3rem;
  border-bottom: 1px solid var(--rule);
}

h3 {
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--muted);
  margin: 1.25rem 0 0.5rem;
}

.note { color: var(--muted); font-size: 0.875rem; margin: 0 0 0.75rem; }

/* ---- the entry row, with the dot leader from the printed sheet ---------- */

.entry {
  display: grid;
  grid-template-columns: minmax(6rem, auto) 1fr minmax(45%, 1fr);
  align-items: baseline;
  column-gap: 0.4rem;
  padding: 0.3rem 0;
}

.entry + .entry { border-top: 1px solid color-mix(in srgb, var(--rule) 45%, transparent); }

.entry dt { font-weight: 600; font-size: 0.9375rem; }

.entry .fill {
  border-bottom: 2px dotted var(--leader);
  transform: translateY(-0.28em);
  min-width: 1.5rem;
}

.entry dd { margin: 0; font-size: 0.9375rem; text-wrap: pretty; }

.status { font-weight: 600; }
.status.closed  { color: var(--closed); }
.status.caution { color: var(--caution); }
.status.open    { color: var(--open); }

/* On a narrow screen the leader has nowhere to go, so the row stacks. */
@media (max-width: 34rem) {
  .entry { display: block; padding: 0.55rem 0; }
  .entry .fill { display: none; }
  .entry dd { margin-top: 0.1rem; }
}

/* ---- footer ------------------------------------------------------------ */

footer {
  margin-top: 3rem;
  padding-top: 1rem;
  border-top: 1px solid var(--rule);
  font-size: 0.8125rem;
  color: var(--muted);
}

footer p { margin: 0 0 0.5rem; }

footer a { color: inherit; text-decoration-color: var(--rule); }
footer a:hover { text-decoration-color: currentColor; }

.downloads { display: flex; gap: 1.25rem; flex-wrap: wrap; margin-bottom: 1rem; }
.downloads a {
  font-size: 0.9375rem;
  color: var(--ink);
  text-decoration-thickness: 1px;
  text-underline-offset: 3px;
}

:focus-visible { outline: 2px solid var(--ink); outline-offset: 3px; }

@media print {
  body { background: #fff; color: #000; padding: 0; font-size: 10pt; }
  .downloads, .provenance { border: 0; }
  .entry .fill { border-bottom-color: #bbb; }
}
"""

# How the schedule is described to readers. Deliberately vague: GitHub queues
# cron jobs and can run them up to an hour late, so naming exact clock times
# would promise a precision the schedule does not have. The edition time in
# the masthead is the real build time, which is the part worth being exact
# about.
SCHEDULE = "in the morning and the evening"

# Where a reader should go to check something properly.
OFFICIAL = {
    "Weather": "https://forecast.weather.gov/MapClick.php?zoneid=CAZ522",
    "Roads": "https://www.nps.gov/deva/planyourvisit/conditions.htm",
    "Campgrounds": "https://www.nps.gov/deva/planyourvisit/developed-campgrounds.htm",
}


FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?'
    'family=IBM+Plex+Sans:wght@400;600&'
    'family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400'
    '&display=swap" rel="stylesheet">'
)


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

def edition_label(built):
    """Which edition this build is, by the clock it was actually run at."""
    h = built.hour
    if 5 <= h < 12:
        return "Morning edition"
    if 12 <= h < 17:
        return "Afternoon edition"
    if 17 <= h < 21:
        return "Evening edition"
    return "Night edition"


def e(s):
    return html.escape(str(s if s is not None else ""))


def status_class(status):
    """CAUTION is not a synonym for open, and must not be coloured like one."""
    s = (status or "").upper()
    if "CLOSED" in s:
        return "closed"
    if "CAUTION" in s or "PARTIALLY" in s or "DELAY" in s:
        return "caution"
    return "open"


def entry(label, value, status=None):
    """One label/value row with a dot leader between them."""
    if status:
        cls = status_class(status)
        value = f'<span class="status {cls}">{e(status)}.</span> {e(value)}'.rstrip()
    else:
        value = e(value)
    return (f'<div class="entry"><dt>{e(label)}</dt>'
            f'<span class="fill" aria-hidden="true"></span>'
            f'<dd>{value}</dd></div>')


def items_block(items):
    out = []
    for it in items:
        detail = (it.get("detail") or "").strip()
        if detail:
            detail = detail[0].upper() + detail[1:]
        # With a status and nothing more to say, the status is the whole story.
        fallback = "" if it.get("status") else "See the park website."
        out.append(entry(it["name"], detail or fallback, it.get("status")))
    return "".join(out)


def build_html(rep, *, pdf_name, txt_name):
    parts = []

    # --- masthead --------------------------------------------------------
    built = datetime.fromisoformat(rep.generated)
    edition = edition_label(built)
    links = ", ".join(
        f'<a href="{e(url)}">{e(name)}</a>' for name, url in OFFICIAL.items())
    # "A, B and C" rather than a trailing comma before the last link.
    if links.count("</a>, ") >= 1:
        head, _, tail = links.rpartition(", ")
        links = f"{head} and {tail}"
    parts.append(
        '<header>'
        '<h1>Death Valley National Park</h1>'
        f'<p class="report-date">{e(rep.report_date)}</p>'
        f'<p class="provenance">{e(edition)}. '
        'Not produced by or affiliated with the National Park Service or Death '
        'Valley National Park. Check official sources before you travel: '
        f'{links}.</p>'
        '</header>'
    )

    # --- lede: whichever period is closest to now ------------------------
    lede = None
    for slot in ("Today", "Tonight", "Tomorrow"):
        if rep.forecast.get(slot):
            lede = (slot, rep.forecast[slot]["text"])
            break
    if lede:
        parts.append(f'<div class="lede"><p class="period">{e(lede[0])}</p>'
                     f'<p>{e(lede[1])}</p></div>')

    parts.append(
        '<p class="sun">'
        f'<span>Sunset today <b>{e(rep.sunset_today)}</b></span>'
        f'<span>Sunrise tomorrow <b>{e(rep.sunrise_tomorrow)}</b></span>'
        '</p>'
    )

    # --- remaining forecast periods --------------------------------------
    rest = [(s, rep.forecast[s]["text"])
            for s in ("Today", "Tonight", "Tomorrow", "Tomorrow night", "Extended")
            if rep.forecast.get(s) and (not lede or s != lede[0])]
    if rest:
        parts.append('<section><h2>Forecast</h2>'
                     + "".join(entry(s, t) for s, t in rest) + '</section>')
    elif not lede:
        parts.append('<section><h2>Forecast</h2>'
                     '<p class="note">The forecast did not come through on this '
                     'build.</p></section>')

    # --- conditions ------------------------------------------------------
    cur = rep.current
    rows = []
    if cur and cur.get("temp_f") is not None:
        bits = [f"{cur['temp_f']:.0f}&deg;F ({mr.c_from_f(cur['temp_f'])}&deg;C)"]
        if cur.get("wind"):
            bits.append(f"wind {e(cur['wind'])}")
        if cur.get("humidity"):
            bits.append(f"humidity {e(cur['humidity'])}")
        line = ", ".join(bits)
        if cur.get("updated"):
            line += f" (as of {e(cur['updated'])})"
        rows.append(f'<div class="entry"><dt>Right now</dt>'
                    f'<span class="fill" aria-hidden="true"></span>'
                    f'<dd>{line}</dd></div>')

    for d in rep.daily_climate:
        if d.get("note"):
            rows.append(entry(d["label"], f"[{d['note']}]"))
            continue
        hi, lo = d.get("high_f"), d.get("low_f")
        hi_s = (f"{hi:.0f}&deg;F ({mr.c_from_f(hi)}&deg;C)"
                if hi is not None else "n/a")
        lo_s = (f"{lo:.0f}&deg;F ({mr.c_from_f(lo)}&deg;C)"
                if lo is not None else "n/a")
        rows.append(f'<div class="entry"><dt>{e(d["label"])}</dt>'
                    f'<span class="fill" aria-hidden="true"></span>'
                    f'<dd>High {hi_s} &nbsp; Low {lo_s} &nbsp; '
                    f'Precip {e(d.get("precip") or "n/a")}</dd></div>')
    if rows:
        parts.append('<section><h2>Temperature and precipitation</h2>'
                     '<p class="note">Last 24 hours.</p>'
                     + "".join(rows) + '</section>')

    # --- year to date ----------------------------------------------------
    ytd = rep.year_to_date or {}
    if ytd and not ytd.get("note"):
        hi, lo = ytd.get("high_f"), ytd.get("low_f")
        t = []
        if hi is not None:
            t.append(f"High {hi:.0f}&deg;F ({mr.c_from_f(hi)}&deg;C) "
                     f"on {e(mrp.pretty_date(ytd['high_date']))}")
        if lo is not None:
            t.append(f"Low {lo:.0f}&deg;F ({mr.c_from_f(lo)}&deg;C) "
                     f"on {e(mrp.pretty_date(ytd['low_date']))}")
        precip = (f"Calendar year {ytd.get('calendar_precip', 0):.2f} in "
                  f"&nbsp; Since {e(mrp.pretty_date(ytd.get('water_year_start')))} "
                  f"{ytd.get('water_year_precip', 0):.2f} in")
        gap = ytd.get("missing_days") or 0
        if gap:
            precip += (f' <span class="note">&mdash; {gap} '
                       f'day{"s" if gap != 1 else ""} not reported, so the '
                       f'total runs low</span>')
        parts.append(
            f'<section><h2>Year to date at {e(ytd.get("label", "Furnace Creek"))}</h2>'
            f'<div class="entry"><dt>Temperature</dt>'
            f'<span class="fill" aria-hidden="true"></span>'
            f'<dd>{" &nbsp; ".join(t) or "n/a"}</dd></div>'
            f'<div class="entry"><dt>Precipitation</dt>'
            f'<span class="fill" aria-hidden="true"></span>'
            f'<dd>{precip}</dd></div></section>')

    # --- roads -----------------------------------------------------------
    road_parts = []
    if rep.roads_updated:
        road_parts.append(f'<p class="note">Park road status updated '
                          f'{e(rep.roads_updated)}.</p>')
    if rep.roads_paved:
        road_parts.append('<h3>Paved</h3>' + items_block(rep.roads_paved))
    if rep.roads_unpaved:
        road_parts.append('<h3>Unpaved and backcountry</h3>'
                          + items_block(rep.roads_unpaved))
    if not (rep.roads_paved or rep.roads_unpaved):
        road_parts.append('<p class="note">Road status did not come through on '
                          'this build.</p>')
    parts.append('<section><h2>Roads</h2>' + "".join(road_parts) + '</section>')

    if rep.sierra:
        parts.append('<section><h2>Sierra Nevada passes</h2>'
                     + "".join(entry(f"{s['road']} \u2014 {s['name']}",
                                     s["status"]) for s in rep.sierra)
                     + '</section>')

    parts.append('<section><h2>Campgrounds</h2>'
                 + (items_block(rep.campgrounds) if rep.campgrounds
                    else '<p class="note">Campground status did not come '
                         'through on this build.</p>')
                 + '</section>')

    if rep.facilities:
        parts.append('<section><h2>Facilities</h2>'
                     + items_block(rep.facilities) + '</section>')

    # --- footer ----------------------------------------------------------
    stamp = mr.stamp(built, "%A, %B %-d, %Y at %-I:%M %p",
                     "%A, %B %d, %Y at %I:%M %p")
    stamp = stamp.replace("AM", "am").replace("PM", "pm")
    parts.append(
        '<footer>'
        f'<p class="downloads"><a href="{e(pdf_name)}">Printable PDF</a>'
        f'<a href="{e(txt_name)}">Plain text</a></p>'
        f'<p>Built twice daily, {e(SCHEDULE)}. '
        f'Most recent build: {e(stamp)} Pacific.</p>'
        '<p>Sources: '
        '<a href="https://www.nps.gov/deva/planyourvisit/conditions.htm">'
        'NPS Alerts &amp; Conditions</a>, '
        '<a href="https://forecast.weather.gov/MapClick.php?zoneid=CAZ522">'
        'NWS zone CAZ522</a>, '
        '<a href="https://www.rcc-acis.org/">RCC-ACIS</a>, '
        '<a href="https://roads.dot.ca.gov/">Caltrans</a>.</p>'
        '</footer>'
    )

    body = "".join(parts)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Death Valley National Park &middot; {e(rep.report_date)}</title>'
        '<meta name="description" content="An unofficial daily reconstruction '
        'of the Death Valley National Park Morning Report: forecast, road '
        'conditions and campground status.">'
        '<meta name="color-scheme" content="light dark">'
        f'{FONT_LINK}<style>{CSS}</style></head>'
        f'<body><main class="sheet">{body}</main></body></html>'
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="build_site",
        description="Build the Morning Report page, PDF and text file.")
    ap.add_argument("--out", default="site", help="output directory (default: site)")
    ap.add_argument("--no-sierra", action="store_true")
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)

    rep = mr.build_report(skip_sierra=args.no_sierra)

    pdf_name, txt_name = "morning-report.pdf", "morning-report.txt"

    html_path = os.path.join(args.out, "index.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(build_html(rep, pdf_name=pdf_name, txt_name=txt_name))

    txt_path = os.path.join(args.out, txt_name)
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(mr.render_text(rep) + "\n")

    pdf_path = os.path.join(args.out, pdf_name)
    mrp.render(rep, pdf_path, verbose=False)

    print(f"Wrote {html_path}, {txt_path}, {pdf_path}")

    if rep.errors:
        print("\nSources that did not respond:", file=sys.stderr)
        for err in rep.errors:
            print(f"  - {err}", file=sys.stderr)
        # A build with no roads and no forecast is not worth publishing.
        if not rep.forecast and not rep.roads_paved and not rep.roads_unpaved:
            print("Nothing substantial was fetched.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
