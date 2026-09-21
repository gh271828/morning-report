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
import re
import os
import sys
from datetime import datetime

try:
    import morning_report as mr
    import morning_report_pdf as mrp
except ImportError as e:
    sys.exit(f"Needs morning_report.py and morning_report_pdf.py alongside it ({e}).")


# ---------------------------------------------------------------------------
# Design: the park's printed sheet, on screen
#
# Black bar and heavy rule framing the masthead, bold section heads between
# rules, rows indented beneath them with dotted leaders, multi-column
# temperature rows and a two-column road list -- the October 2015 sheet. On a
# phone the columns and leaders give way to a single stacked list, because a
# leader needs width to mean anything.
# ---------------------------------------------------------------------------

# How the schedule is described to readers. Deliberately vague: GitHub queues
# cron jobs and can run them up to an hour late, so naming exact clock times
# would promise a precision the schedule does not have. The exact build time
# is stated in the footer.
SCHEDULE = "in the morning and the evening"


CSS = """
:root {
  --paper: #ffffff;
  --ink: #111111;
  --muted: #5c5c5c;
  --leader: #8c8c8c;
}
@media (prefers-color-scheme: dark) {
  :root { --paper: #141414; --ink: #ececec; --muted: #a2a2a2; --leader: #666666; }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  padding: 1.5rem 1rem 3rem;
  background: var(--paper);
  color: var(--ink);
  font: 15px/1.42 "Helvetica Neue", Helvetica, Arial, sans-serif;
}
.sheet { max-width: 57rem; margin: 0 auto; }
a { color: inherit; text-underline-offset: 2px; }

/* masthead */
.bar { height: 0.85rem; background: var(--ink); }
h1 {
  font-size: clamp(1.7rem, 5.6vw, 2.55rem);
  font-weight: 700;
  letter-spacing: -0.01em;
  line-height: 1.1;
  margin: 0.55rem 0 0.15rem;
}
.edition { font-size: clamp(1.15rem, 3.7vw, 1.6rem); margin: 0; }
.edition b { font-weight: 700; }
.prov {
  font-size: 0.78rem;
  font-style: italic;
  color: var(--muted);
  margin: 0.4rem 0 0;
  padding-bottom: 0.6rem;
  border-bottom: 4px solid var(--ink);
}

/* sections */
section { border-top: 2px solid var(--ink); padding: 0.6rem 0 0.75rem; }
section.first { border-top: 0; }
h2 { font-size: 1.08rem; margin: 0 0 0.4rem; }
h2 small { font-weight: 400; font-size: 0.72rem; margin-left: 0.3rem; }
h3 { font-size: 0.97rem; margin: 1rem 0 0.35rem; }

/* a label, a dotted leader, a value */
.row {
  display: grid;
  grid-template-columns: 13rem 1fr;
  padding-left: 1.1rem;
  margin: 0.12rem 0;
  font-size: 0.88rem;
  align-items: start;
}
/* Labels and leaders sit on the value's first line, as on the sheet. */
.lab { display: flex; align-items: baseline; min-width: 0; padding-right: 0.35rem; }
.lab .t { flex: 0 1 auto; }
.lab::after, .seg::after {
  content: "";
  flex: 1 0 1rem;
  border-bottom: 2px dotted var(--leader);
  margin: 0 0 0 0.3em;
  align-self: baseline;
  height: 0.3em;
}
.row.cont .lab::after, .lab.empty::after, .seg.last::after { display: none; }
.val b { font-weight: 700; }
.val .muted { color: var(--muted); }

/* temperature rows: up to three values, each led into the next */
.segs { display: grid; grid-template-columns: 14rem 15.5rem auto; }
.seg { display: flex; align-items: baseline; padding-right: 0.35rem; }
.seg .t { white-space: nowrap; }

/* Too narrow for three value columns: stack them. */
@media (max-width: 60rem) {
  .segs { grid-template-columns: 1fr; }
  .seg::after { display: none; }
}

.note { padding-left: 1.1rem; margin: 0.12rem 0; font-size: 0.88rem; }
.note.i { font-style: italic; }
.small { padding-left: 14.1rem; font-size: 0.76rem; font-style: italic; color: var(--muted); margin: 0.1rem 0; }

/* two-column lists: roads, campgrounds, facilities */
.twocol { columns: 2; column-gap: 1.8rem; padding-left: 1.1rem; margin-top: 0.5rem; }
.twocol .row { grid-template-columns: 9.5rem 1fr; padding-left: 0; margin: 0 0 0.35rem; break-inside: avoid; }

/* footer */
footer { border-top: 3px solid var(--ink); padding-top: 0.6rem; font-size: 0.82rem; }
footer p { margin: 0 0 0.4rem; }
footer .i { font-style: italic; }
.dl a { margin-right: 1.3rem; font-size: 0.9rem; }

/* phones: one column, no leaders, labels above their values */
@media (max-width: 42rem) {
  .row, .twocol .row { grid-template-columns: 1fr; padding-left: 0.4rem; margin: 0.35rem 0; }
  .lab::after, .seg::after { display: none; }
  .lab .t { font-weight: 600; }
  .segs { grid-template-columns: 1fr; }
  .twocol { columns: 1; padding-left: 0; }
  .note { padding-left: 0.4rem; }
  .small { padding-left: 0.4rem; }
}

:focus-visible { outline: 2px solid var(--ink); outline-offset: 3px; }
@media print { body { padding: 0; font-size: 11px; } .dl { display: none; } }
"""


def e(s):
    return html.escape(str(s if s is not None else ""))


def status_html(status, detail=""):
    """'CLOSED' + 'Due to flooding' -> '<b>CLOSED.</b> Due to flooding'."""
    detail = (detail or "").strip()
    if detail:
        detail = detail[0].upper() + detail[1:]
    if status:
        return f"<b>{e(status)}.</b> {e(detail)}".rstrip()
    return e(detail or "See the park website.")


def row(label, value_html, cls=""):
    lab = (f'<span class="lab"><span class="t">{e(label)}</span></span>' if label
           else '<span class="lab empty"></span>')
    return f'<div class="row {cls}">{lab}<div class="val">{value_html}</div></div>'


def multi(label, segs):
    cells = "".join(
        f'<span class="seg{" last" if i == len(segs) - 1 else ""}">'
        f'<span class="t">{e(seg)}</span></span>'
        for i, seg in enumerate(segs))
    lab = (f'<span class="lab"><span class="t">{e(label)}</span></span>' if label
           else '<span class="lab empty"></span>')
    return f'<div class="row">{lab}<div class="segs">{cells}</div></div>'


def twocol(pairs):
    return '<div class="twocol">' + "".join(row(l, v) for l, v in pairs) + "</div>"


def _road_pair(it):
    """Mirror the sheet: a long parenthetical moves from the name into the value."""
    name, detail = it["name"], it.get("detail") or ""
    m = re.match(r"^(.*?)\s*\((.+)\)\s*$", name)
    if m and len(name) > 26:
        name = m.group(1).strip()
        lead = m.group(2).strip()
        detail = f"{lead[0].upper() + lead[1:]}. {detail}".strip()
    return name, status_html(it.get("status"), detail)


def build_html(rep, *, pdf_name, txt_name):
    P = []
    built = datetime.fromisoformat(rep.generated)
    edition = mr.edition_label(built)

    # --- masthead ----------------------------------------------------------
    names = list(mr.OFFICIAL)
    links = [f'<a href="{e(mr.OFFICIAL[n])}">{e(n)}</a>' for n in names]
    linked = ", ".join(links[:-1]) + f" and {links[-1]}" if len(links) > 1 else links[0]
    P.append(
        '<header><div class="bar"></div>'
        '<h1>Death Valley National Park</h1>'
        f'<p class="edition"><b>{e(edition.title())}:</b> '
        f'{e(rep.report_date.replace(",", "", 1))}</p>'
        f'<p class="prov">{e(mr.DISCLAIMER[:-1])}: {linked}.</p>'
        '</header>')

    # --- weather forecast --------------------------------------------------
    rows = [row(slot, e(rep.forecast[slot]["text"]))
            for slot in ("Today", "Tonight", "Tomorrow", "Tomorrow night", "Extended")
            if rep.forecast.get(slot)]
    if not rows:
        rows = ['<p class="note">The forecast did not come through on this build.</p>']
    rows.append(multi("", [f"Sunset today: {rep.sunset_today}",
                           f"Sunrise tomorrow: {rep.sunrise_tomorrow}"]))
    P.append('<section class="first"><h2>Weather Forecast</h2>' + "".join(rows) + "</section>")

    # --- temperatures, last 24 hours ---------------------------------------
    rows = []
    for d in rep.daily_climate:
        if d.get("note"):
            rows.append(row(d["label"], f'<span class="muted">[{e(d["note"])}]</span>'))
            continue
        hi, lo = d.get("high_f"), d.get("low_f")
        rows.append(multi(d["label"], [
            f"High: {hi:.0f}\u00b0F ({mr.c_from_f(hi)}\u00b0C)" if hi is not None else "High: n/a",
            f"Low: {lo:.0f}\u00b0F ({mr.c_from_f(lo)}\u00b0C)" if lo is not None else "Low: n/a",
            f"Precipitation: {d.get('precip') or 'n/a'}"]))
    cur = rep.current
    if cur and cur.get("temp_f") is not None:
        bits = [f"{cur['temp_f']:.0f}\u00b0F ({mr.c_from_f(cur['temp_f'])}\u00b0C)"]
        if cur.get("wind"):
            bits.append(f"wind {cur['wind']}")
        if cur.get("humidity"):
            bits.append(f"humidity {cur['humidity']}")
        line = ", ".join(bits) + (f" (as of {cur['updated']})" if cur.get("updated") else "")
        rows.append(row("Now at Furnace Creek", e(line)))
    if rows:
        P.append('<section><h2>Temperatures &amp; Precipitation <small>(last 24 hours)</small></h2>'
                 + "".join(rows) + "</section>")

    # --- year to date ------------------------------------------------------
    ytd = rep.year_to_date or {}
    if ytd and not ytd.get("note"):
        hi, lo = ytd.get("high_f"), ytd.get("low_f")
        segs = []
        if hi is not None:
            segs.append(f"High: {hi:.0f}\u00b0F ({mr.c_from_f(hi)}\u00b0C) on {mrp.short_date(ytd['high_date'])}")
        if lo is not None:
            segs.append(f"Low: {lo:.0f}\u00b0F ({mr.c_from_f(lo)}\u00b0C) on {mrp.short_date(ytd['low_date'])}")
        year = rep.report_date.split()[-1]
        rows = [multi("Temperatures", segs or ["n/a"]),
                multi("Precipitation", [
                    f"Year {year}: {ytd.get('calendar_precip', 0):.2f} inches",
                    f"{mrp.short_date(ytd.get('water_year_start'))} through today: "
                    f"{ytd.get('water_year_precip', 0):.2f} inches"])]
        gap = ytd.get("missing_days") or 0
        if gap:
            rows.append(f'<p class="small">{gap} day{"s" if gap != 1 else ""} not reported '
                        'this year, so the totals may run low.</p>')
        P.append(f'<section><h2>Year to Date <small>({e(ytd.get("label", "Furnace Creek"))})</small></h2>'
                 + "".join(rows) + "</section>")

    # --- roads -------------------------------------------------------------
    R = ['<h2>Current Road Conditions</h2>']
    note = mr.road_status_note(rep)
    if note:
        R.append(f'<p class="note">{e(note)}</p>')
    R.append(f'<p class="note i">For more park road information visit the '
             f'<a href="{e(mr.OFFICIAL["Roads"])}">NPS Alerts &amp; Conditions page</a>.</p>')
    roads = rep.roads_paved + rep.roads_unpaved
    if roads:
        R.append(twocol(sorted((_road_pair(it) for it in roads), key=lambda t: t[0].lower())))
    else:
        R.append('<p class="note">Road status did not come through on this build.</p>')

    if rep.approach:
        R.append("<h3>Roads OUTSIDE Death Valley</h3>")
        for a in rep.approach:
            if a["road"]:
                R.append(row(f"CA {a['road']}",
                             f'{e(a["status"])} <span class="muted">\u2014 {e(a["name"])}</span>'))
            else:
                R.append(row("", e(a["status"]), "cont"))

    if rep.sierra:
        R.append("<h3>Sierra Nevada Roads</h3>")
        for sp in rep.sierra:
            R.append(row(f"CA {sp['road']}",
                         f'{e(sp["status"])} <span class="muted">\u2014 {e(sp["name"])}</span>'))
    P.append("<section>" + "".join(R) + "</section>")

    # --- campgrounds and facilities ---------------------------------------
    if rep.campgrounds:
        P.append('<section><h2>Campgrounds</h2>'
                 + twocol([(mrp.short_label(it["name"]), status_html(it.get("status"), it.get("detail")))
                           for it in rep.campgrounds])
                 + "</section>")
    else:
        P.append('<section><h2>Campgrounds</h2><p class="note">Campground status did not '
                 'come through on this build.</p></section>')
    if rep.facilities:
        P.append('<section><h2>Facilities</h2>'
                 + twocol([(it["name"], status_html(it.get("status"), it.get("detail")))
                           for it in rep.facilities])
                 + "</section>")

    # --- footer ------------------------------------------------------------
    stamp = mr.stamp(built, "%A, %B %-d, %Y at %-I:%M %p", "%A, %B %d, %Y at %I:%M %p")
    stamp = stamp.replace("AM", "am").replace("PM", "pm")
    P.append(
        '<footer>'
        f'<p class="i">Built twice daily, {e(SCHEDULE)}. Most recent build: {e(stamp)} Pacific. '
        'Conditions are subject to change without notice.</p>'
        f'<p class="dl"><a href="{e(pdf_name)}">Printable PDF</a>'
        f'<a href="{e(txt_name)}">Plain text</a></p>'
        '<p>Sources: '
        '<a href="https://www.nps.gov/deva/planyourvisit/conditions.htm">NPS Alerts &amp; Conditions</a>, '
        '<a href="https://forecast.weather.gov/MapClick.php?zoneid=CAZ522">NWS zone CAZ522</a>, '
        '<a href="https://www.rcc-acis.org/">RCC-ACIS</a>, '
        '<a href="https://roads.dot.ca.gov/">Caltrans</a>.</p>'
        '</footer>')

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Death Valley National Park &middot; {e(rep.report_date)}</title>'
        '<meta name="description" content="An unofficial daily reconstruction of the '
        'Death Valley National Park morning report: forecast, road conditions and '
        'campground status.">'
        '<meta name="color-scheme" content="light dark">'
        f'<style>{CSS}</style></head>'
        f'<body><main class="sheet">{"".join(P)}</main></body></html>')


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
