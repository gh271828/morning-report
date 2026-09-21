#!/usr/bin/env python3
"""
Morning Report -- PDF
=====================

Companion to morning_report.py. Fetches the same data and lays it out on a
single Letter page, with the print date and time along the bottom.

    $ python3 morning_report_pdf.py
    $ python3 morning_report_pdf.py -o ~/Desktop/report.pdf --open

Both files must sit in the same folder; this one imports the other.

Requires: requests, beautifulsoup4, reportlab
    python3 -m pip install requests beautifulsoup4 reportlab

Fitting to one page
-------------------
Content length varies a lot -- a winter day with six closed backcountry roads
runs far longer than a quiet one. So the layout is measured before it is drawn:
the body type shrinks a quarter point at a time until everything fits, and if
it still will not fit at the floor, the lowest-priority sections are dropped in
order and a note is printed to the terminal saying what came out. Nothing is
ever silently cut off mid-sentence.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime

try:
    import morning_report as mr
except ImportError:
    sys.exit("Put morning_report_pdf.py in the same folder as morning_report.py.")

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas as rl_canvas
except ImportError:
    sys.exit("Missing dependency: python3 -m pip install reportlab")


# ---------------------------------------------------------------------------
# Page geometry
# ---------------------------------------------------------------------------

PAGE_W, PAGE_H = letter
MARGIN_X = 0.6 * inch
MARGIN_TOP = 0.55 * inch
MARGIN_BOT = 0.78 * inch         # footer rule, print stamp, and clear air above

BODY = "Helvetica"
BOLD = "Helvetica-Bold"

LABEL_W = 1.95 * inch            # width of the label column
GUTTER = 0.10 * inch

MAX_BODY_PT = 10.0
MIN_BODY_PT = 6.25
PT_STEP = 0.25

# Sections shed, in this order, if the page still overflows at minimum type.
# Least load-bearing first: the Sierra passes matter most on exactly the bad
# winter days that make the page overflow, so they go last.
DROPPABLE = ["facilities", "current", "sierra"]


# ---------------------------------------------------------------------------
# Block model -- the report as a flat list of drawable pieces
# ---------------------------------------------------------------------------

def pretty_date(iso):
    """'2026-07-12' -> 'July 12'. Left alone if it is not an ISO date."""
    try:
        d = datetime.strptime(str(iso), "%Y-%m-%d")
    except (ValueError, TypeError):
        return str(iso)
    return mr.stamp(d, "%B %-d", "%B %d")


def short_label(name):
    """
    'Thorndike (Primitive Campground)' -> 'Thorndike (primitive)'. The section
    heading already says Campgrounds, so the repetition only costs line breaks.
    NPS's own misspellings are normalised here too, since this is a label.
    """
    import re as _re
    name = _re.sub(r"\(\s*Primitive\s+Camp?r?ground\s*\)", "(primitive)",
                   name, flags=_re.I)
    name = _re.sub(r"\s+Camp?r?ground$", "", name, flags=_re.I)
    return name.strip()


def _pairs_from_items(items, shorten=False):
    out = []
    for it in items:
        name = short_label(it["name"]) if shorten else it["name"]
        status = it.get("status")
        detail = (it.get("detail") or "").strip()
        if detail:
            detail = detail[0].upper() + detail[1:]
        value = f"{status}. {detail}".strip() if status else detail
        out.append((name, value or "See park website."))
    return out


def build_blocks(rep, include=None):
    """
    Turn a Report into ('kind', payload) tuples. Kinds:
        title, subtitle, heading, subheading, pair, centre, rule, note, gap
    """
    inc = include if include is not None else set(DROPPABLE)
    B = []
    B.append(("title", "Death Valley National Park"))
    B.append(("subtitle", rep.report_date))
    edition = mr.edition_label(datetime.fromisoformat(rep.generated))
    B.append(("provenance", f"{edition}. {mr.DISCLAIMER}"))
    B.append(("gap", 7))

    B.append(("heading", "Weather Forecast"))
    got = False
    for slot in ("Today", "Tonight", "Tomorrow", "Tomorrow night", "Extended"):
        p = rep.forecast.get(slot)
        if p:
            if got:
                B.append(("gap", 2.5))
            B.append(("pair", (slot, p["text"])))
            got = True
    if not got:
        B.append(("note", "Forecast unavailable."))
    B.append(("gap", 3))
    B.append(("centre", f"Sunset today: {rep.sunset_today}"
                        f"     Sunrise tomorrow: {rep.sunrise_tomorrow}"))
    B.append(("gap", 5))

    cur = rep.current
    if "current" in inc and cur and cur.get("temp_f") is not None:
        bits = [f"{cur['temp_f']:.0f}\u00b0F ({mr.c_from_f(cur['temp_f'])}\u00b0C)"]
        if cur.get("wind"):
            bits.append(f"wind {cur['wind']}")
        if cur.get("humidity"):
            bits.append(f"humidity {cur['humidity']}")
        line = ", ".join(bits)
        if cur.get("updated"):
            line += f" (as of {cur['updated']})"
        B.append(("heading", "Currently at Furnace Creek"))
        B.append(("pair", ("Observed", line)))
        B.append(("gap", 5))

    B.append(("heading", "Temperatures & Precipitation (last 24 hours)"))
    if rep.daily_climate:
        for d in rep.daily_climate:
            if d.get("note"):
                B.append(("pair", (d["label"], f"[{d['note']}]")))
                continue
            hi, lo = d.get("high_f"), d.get("low_f")
            hi_s = (f"{hi:.0f}\u00b0F ({mr.c_from_f(hi)}\u00b0C)"
                    if hi is not None else "n/a")
            lo_s = (f"{lo:.0f}\u00b0F ({mr.c_from_f(lo)}\u00b0C)"
                    if lo is not None else "n/a")
            B.append(("pair", (d["label"],
                               f"High: {hi_s}   \u00b7   Low: {lo_s}   \u00b7   "
                               f"Precip: {d.get('precip') or 'n/a'}")))
    else:
        B.append(("note", "Unavailable."))
    B.append(("gap", 5))

    ytd = rep.year_to_date or {}
    B.append(("heading", f"Year to Date ({ytd.get('label', 'Furnace Creek')})"))
    if ytd and not ytd.get("note"):
        hi, lo = ytd.get("high_f"), ytd.get("low_f")
        t = []
        if hi is not None:
            t.append(f"High: {hi:.0f}\u00b0F ({mr.c_from_f(hi)}\u00b0C) "
                     f"on {pretty_date(ytd['high_date'])}")
        if lo is not None:
            t.append(f"Low: {lo:.0f}\u00b0F ({mr.c_from_f(lo)}\u00b0C) "
                     f"on {pretty_date(ytd['low_date'])}")
        B.append(("pair", ("Temperatures", "   \u00b7   ".join(t) or "n/a")))
        precip = (f"Calendar year: {ytd.get('calendar_precip', 0):.2f} in"
                  f"   \u00b7   Since {pretty_date(ytd.get('water_year_start'))}: "
                  f"{ytd.get('water_year_precip', 0):.2f} in")
        gap = ytd.get("missing_days") or 0
        if gap:
            precip += f"   \u00b7   {gap} day{'s' if gap != 1 else ''} not reported"
        B.append(("pair", ("Precipitation", precip)))
    else:
        B.append(("note", "Unavailable."))
    B.append(("gap", 5))

    B.append(("heading", "Current Road Conditions"))
    if rep.roads_updated:
        B.append(("note", f"Park road status updated {rep.roads_updated}."))
    if rep.roads_paved:
        B.append(("subheading", "Paved Roads"))
        B += [("pair", p) for p in _pairs_from_items(rep.roads_paved)]
    if rep.roads_unpaved:
        B.append(("subheading", "Unpaved & Backcountry Roads"))
        B += [("pair", p) for p in _pairs_from_items(rep.roads_unpaved)]
    if not (rep.roads_paved or rep.roads_unpaved):
        B.append(("note", "Unavailable."))
    B.append(("gap", 5))

    if "sierra" in inc and rep.sierra:
        B.append(("heading", "Sierra Nevada Roads"))
        for s in rep.sierra:
            B.append(("pair", (f"{s['road']} ({s['name']})", s["status"])))
        B.append(("gap", 5))

    B.append(("heading", "Campgrounds"))
    if rep.campgrounds:
        B += [("pair", p) for p in _pairs_from_items(rep.campgrounds, shorten=True)]
    else:
        B.append(("note", "Unavailable."))

    if "facilities" in inc and rep.facilities:
        B.append(("gap", 5))
        B.append(("heading", "Facilities & Popular Locations"))
        B += [("pair", p) for p in _pairs_from_items(rep.facilities)]

    return B


# ---------------------------------------------------------------------------
# Measuring and drawing
# ---------------------------------------------------------------------------

def wrap(text, font, size, width):
    """Greedy wrap by measured string width."""
    words = text.split()
    if not words:
        return [""]
    lines, cur = [], words[0]
    for w in words[1:]:
        trial = cur + " " + w
        if pdfmetrics.stringWidth(trial, font, size) <= width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def layout(blocks, pt):
    """
    Measure at body size `pt`. Returns (total_height, draw_ops) where each op
    is (y_offset_from_top, callable(canvas, y)).
    """
    lead = pt * 1.22
    text_w = PAGE_W - 2 * MARGIN_X - LABEL_W - GUTTER
    full_w = PAGE_W - 2 * MARGIN_X
    x_label = MARGIN_X
    x_text = MARGIN_X + LABEL_W + GUTTER

    ops = []
    y = 0.0

    def op(fn):
        ops.append((y, fn))

    for kind, payload in blocks:
        if kind == "gap":
            y += payload * (pt / 10.0)

        elif kind == "title":
            size = pt + 4.5
            y += size
            txt = payload
            op(lambda c, yy, t=txt, s=size: (
                c.setFont(BOLD, s),
                c.drawCentredString(PAGE_W / 2, yy, t)))
            y += size * 0.35

        elif kind == "subtitle":
            size = pt + 1.5
            y += size
            txt = payload
            op(lambda c, yy, t=txt, s=size: (
                c.setFont(BODY, s),
                c.drawCentredString(PAGE_W / 2, yy, t)))
            y += size * 0.4

        elif kind == "heading":
            y += pt * 1.05
            txt = payload
            op(lambda c, yy, t=txt, s=pt: (
                c.setFont(BOLD, s),
                c.drawString(MARGIN_X, yy, t),
                c.setLineWidth(0.4),
                c.setStrokeGray(0.45),
                c.line(MARGIN_X, yy - s * 0.28,
                       PAGE_W - MARGIN_X, yy - s * 0.28)))
            y += pt * 0.45

        elif kind == "subheading":
            y += pt * 0.45          # separate it from the paragraph above
            y += pt * 1.0
            txt = payload
            op(lambda c, yy, t=txt, s=pt - 0.5: (
                c.setFont(BOLD, s),
                c.drawString(MARGIN_X, yy, t)))
            y += pt * 0.2

        elif kind == "provenance":
            txt = payload
            size = pt - 1.5
            y += pt * 0.25
            for line in wrap(txt, BODY, size, full_w * 0.86):
                y += size * 1.2
                op(lambda c, yy, t=line, sz=size: (
                    c.setFillGray(0.4),
                    c.setFont(BODY, sz),
                    c.drawCentredString(PAGE_W / 2, yy, t),
                    c.setFillGray(0)))
            y += pt * 0.35

        elif kind == "centre":
            y += lead
            txt = payload
            op(lambda c, yy, t=txt, s=pt: (
                c.setFont(BOLD, s),
                c.drawCentredString(PAGE_W / 2, yy, t)))
            y += pt * 0.2

        elif kind == "note":
            txt = payload
            for line in wrap(txt, BODY, pt - 1, full_w):
                y += lead
                op(lambda c, yy, t=line, s=pt - 1: (
                    c.setFillGray(0.4),
                    c.setFont(BODY, s),
                    c.drawString(MARGIN_X, yy, t),
                    c.setFillGray(0)))

        elif kind == "pair":
            label, value = payload
            label_lines = wrap(label, BOLD, pt, LABEL_W)
            value_lines = wrap(value, BODY, pt, text_w)
            rows = max(len(label_lines), len(value_lines))
            start = y
            for i in range(rows):
                y += lead
                ll = label_lines[i] if i < len(label_lines) else None
                vl = value_lines[i] if i < len(value_lines) else None

                def draw(c, yy, ll=ll, vl=vl, s=pt):
                    if ll is not None:
                        c.setFont(BOLD, s)
                        c.drawString(x_label, yy, ll)
                    if vl is not None:
                        c.setFont(BODY, s)
                        c.drawString(x_text, yy, vl)
                op(draw)
            y = start + rows * lead + pt * 0.12

    return y, ops


def render(rep, path, *, verbose=True):
    """Fit and draw. Returns the body point size actually used."""
    avail = PAGE_H - MARGIN_TOP - MARGIN_BOT
    include = set(DROPPABLE)
    dropped = []

    while True:
        blocks = build_blocks(rep, include)
        chosen = None
        pt = MAX_BODY_PT
        while pt >= MIN_BODY_PT - 1e-9:
            height, ops = layout(blocks, pt)
            if height <= avail:
                chosen = (pt, ops)
                break
            pt -= PT_STEP
        if chosen:
            break
        # Still too tall at the smallest size -- shed a section and retry.
        sheddable = [s for s in DROPPABLE if s in include]
        if not sheddable:
            height, ops = layout(blocks, MIN_BODY_PT)
            chosen = (MIN_BODY_PT, ops)
            if verbose:
                print("Warning: content exceeds one page even at minimum size; "
                      "the tail may be clipped.", file=sys.stderr)
            break
        gone = sheddable[0]
        include.discard(gone)
        dropped.append(gone)

    pt, ops = chosen
    c = rl_canvas.Canvas(path, pagesize=letter)
    c.setTitle(f"Death Valley National Park - {rep.report_date}")
    c.setAuthor("Unofficial reconstruction")

    top = PAGE_H - MARGIN_TOP
    for y_off, fn in ops:
        fn(c, top - y_off)

    draw_footer(c, rep, pt)
    c.showPage()
    c.save()

    if dropped and verbose:
        names = {"facilities": "Facilities & Popular Locations",
                 "current": "Currently at Furnace Creek",
                 "sierra": "Sierra Nevada Roads"}
        print("To fit one page, omitted: "
              + ", ".join(names[d] for d in dropped), file=sys.stderr)
    return pt


def draw_footer(c, rep, pt):
    size = max(6.0, pt - 2.0)
    y = MARGIN_BOT - 0.16 * inch

    c.setLineWidth(0.4)
    c.setStrokeGray(0.45)
    c.line(MARGIN_X, y + size * 1.9, PAGE_W - MARGIN_X, y + size * 1.9)

    printed = datetime.now(mr.PARK_TZ)
    stamp = mr.stamp(printed, "%A, %B %-d, %Y at %-I:%M %p",
                     "%A, %B %d, %Y at %I:%M %p")
    stamp = stamp.replace("AM", "am").replace("PM", "pm")

    c.setFillGray(0.35)
    c.setFont(BODY, size)
    c.drawString(MARGIN_X, y + size * 0.95,
                 "Weather and road conditions subject to change without notice. "
                 "Sources: NPS, NWS (zone CAZ522), RCC-ACIS, Caltrans.")
    c.drawString(MARGIN_X, y, f"Printed {stamp} Pacific time.")
    c.setFillGray(0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="Morning Report (PDF)",
        description="Build the Death Valley Morning Report as a one-page PDF.")
    ap.add_argument("-o", "--output",
                    help="output path (default: morning_report_YYYY-MM-DD.pdf)")
    ap.add_argument("--no-sierra", action="store_true",
                    help="skip the Caltrans pass lookups")
    ap.add_argument("--open", dest="open_it", action="store_true",
                    help="open the PDF when it is finished")
    args = ap.parse_args(argv)

    rep = mr.build_report(skip_sierra=args.no_sierra)

    path = args.output or (
        f"morning_report_{datetime.now(mr.PARK_TZ).date().isoformat()}.pdf")
    path = os.path.abspath(os.path.expanduser(path))

    pt = render(rep, path)
    print(f"Wrote {path}  (body type {pt:g} pt)")

    if rep.errors:
        print("\nSources that did not respond:", file=sys.stderr)
        for e in rep.errors:
            print(f"  - {e}", file=sys.stderr)

    if args.open_it:
        opener = ("open" if sys.platform == "darwin"
                  else "start" if os.name == "nt" else "xdg-open")
        try:
            subprocess.run([opener, path], check=False)
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
