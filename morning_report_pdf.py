#!/usr/bin/env python3
"""
Morning Report -- PDF
=====================

Companion to morning_report.py. Fetches the same data and lays it out on a
single Letter page modelled on the park's own printed sheet (the October 20,
2015 edition): a black bar and heavy rule framing the masthead, bold section
heads separated by rules, rows indented beneath them with dot leaders,
multi-column rows for temperatures, a two-column list of park roads, and a
single italic line at the foot.

    $ python3 morning_report_pdf.py
    $ python3 morning_report_pdf.py -o ~/Desktop/report.pdf --open

Both files must sit in the same folder; this one imports the other.

Requires: requests, beautifulsoup4, reportlab

Fitting to one page
-------------------
The layout is measured before it is drawn. Body type shrinks a quarter point
at a time until everything fits; if it still will not fit at the floor, the
lowest-priority sections are dropped in order and the terminal says which.
"""

from __future__ import annotations

import argparse
import os
import re
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
# Page geometry, after the 2015 sheet
# ---------------------------------------------------------------------------

PAGE_W, PAGE_H = letter
M_X = 0.6 * inch
M_TOP = 0.45 * inch
M_BOT = 0.45 * inch
RIGHT = PAGE_W - M_X
CONTENT_W = RIGHT - M_X

SANS = "Helvetica"
BOLD = "Helvetica-Bold"
ITAL = "Helvetica-Oblique"

INDENT = 0.25 * inch            # rows sit indented under their section head
VALUE_X = M_X + 2.0 * inch      # values start here in single-column rows
COL2_X = M_X + 3.95 * inch      # second value column (Low, Sunrise)
COL3_X = M_X + 5.75 * inch      # third value column (Precipitation)
GUTTER = 0.3 * inch             # between the two columns of a road list
LABEL2_W = 1.4 * inch           # label width inside a two-column list

FOOTER_H = 0.5 * inch           # reserved for the italic foot line and rule

MAX_PT = 9.5
MIN_PT = 6.25
STEP = 0.25

# Sections shed, in this order, if the page overflows at minimum type.
DROPPABLE = ["facilities", "current", "sierra", "approach"]

# Tried before shedding anything: list plainly-open campgrounds on one line.
COMPACTIONS = ["fold_open_campgrounds"]

SECTION_NAMES = {"facilities": "Facilities",
                 "current": "Now at Furnace Creek",
                 "sierra": "Sierra Nevada Roads",
                 "approach": "Roads OUTSIDE Death Valley"}


# ---------------------------------------------------------------------------
# Small helpers (also used by build_site.py)
# ---------------------------------------------------------------------------

def pretty_date(iso):
    """'2026-07-12' -> 'July 12'. Left alone if it is not an ISO date."""
    try:
        d = datetime.strptime(str(iso), "%Y-%m-%d")
    except (ValueError, TypeError):
        return str(iso)
    return mr.stamp(d, "%B %-d", "%B %d")


def short_date(iso):
    """'2026-07-12' -> '7/12/26', the way the 2015 sheet wrote dates."""
    try:
        d = datetime.strptime(str(iso), "%Y-%m-%d")
    except (ValueError, TypeError):
        return str(iso)
    return f"{d.month}/{d.day}/{d.strftime('%y')}"


def short_label(name):
    """
    'Thorndike (Primitive Campground)' -> 'Thorndike (primitive)'. The section
    heading already says Campgrounds, so the repetition only costs line breaks.
    NPS's own misspellings are normalised here too, since this is a label.
    """
    name = re.sub(r"\(\s*Primitive\s+Camp?r?ground\s*\)", "(primitive)", name, flags=re.I)
    name = re.sub(r"\s+Camp?r?ground$", "", name, flags=re.I)
    return name.strip()


def _is_clear(status):
    s = (status or "").lower()
    return s.startswith("no restrictions") or s.startswith("open. no traffic")


def fold_routes(rows, names=True, prefix=""):
    """
    [{'road','name','status'}, ...] -> [(label, status), ...].

    Routes whose status is an identical all-clear share one row --
    "Hwy 127, 190, 136 ... No restrictions reported." -- because a single page
    has no room to say the same thing four times. Routes with restrictions
    keep their own rows.
    """
    out, clear = [], {}
    for r in rows:
        if not r["road"]:                       # continuation of previous route
            out.append(("", r["status"]))
            continue
        if _is_clear(r["status"]):
            clear.setdefault(r["status"], []).append(r["road"].replace("Hwy ", ""))
            continue
        label = prefix + r["road"]
        if names and r.get("name"):
            label += f" ({r['name']})"
        out.append((label, r["status"]))
    for status, roads in clear.items():
        out.append((f"{prefix}Hwy {', '.join(roads)}", status))
    return out


def _item_value(it):
    detail = (it.get("detail") or "").strip()
    if detail:
        detail = detail[0].upper() + detail[1:]
    status = it.get("status")
    return f"{status}. {detail}".strip() if status else (detail or "See park website.")


def _road_item(it):
    """
    A park road as (label, value) for the two-column list. A long name with a
    parenthetical -- 'Southern Badwater Road (South of Badwater Basin to
    Shoshone)' -- moves the parenthetical into the value, so the narrow label
    column holds just the road's name.
    """
    name = it["name"]
    m = re.match(r"^(.*?)\s*\((.+)\)\s*$", name)
    if m and len(name) > 26:
        name = m.group(1).strip()
        lead = m.group(2).strip()
        it = dict(it, detail=f"{lead[0].upper() + lead[1:]}. {it.get('detail') or ''}".strip())
    return name, _item_value(it)


def wrap(text, font, size, width):
    """Greedy wrap by measured string width."""
    words = (text or "").split()
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


def sw(text, font, size):
    return pdfmetrics.stringWidth(text, font, size)


def draw_dots(c, x0, x1, y, size):
    """A dot leader from x0 to x1, set tight against the value like the original."""
    d = sw(".", SANS, size)
    n = int((x1 - x0) / d)
    if n < 2:
        return
    c.setFont(SANS, size)
    c.drawString(x1 - n * d, y, "." * n)


# ---------------------------------------------------------------------------
# The report as blocks
# ---------------------------------------------------------------------------

def build_blocks(rep, include=None, compact=()):
    inc = include if include is not None else set(DROPPABLE)
    B = []

    # --- Weather forecast ---------------------------------------------------
    B.append(("heading", ("Weather Forecast", None)))
    got = False
    for slot in ("Today", "Tonight", "Tomorrow", "Tomorrow night", "Extended"):
        p = rep.forecast.get(slot)
        if p:
            B.append(("pair", (slot, p["text"])))
            got = True
    if not got:
        B.append(("note", ("Forecast unavailable.", SANS)))
    B.append(("multi", ("", [f"Sunset today: {rep.sunset_today}",
                             f"Sunrise tomorrow: {rep.sunrise_tomorrow}"])))

    # --- Temperatures & precipitation, last 24 hours ------------------------
    B.append(("rule", 1.4))
    B.append(("heading", ("Temperatures & Precipitation", "(last 24 hours)")))
    for d in rep.daily_climate:
        if d.get("note"):
            B.append(("pair", (d["label"], f"[{d['note']}]")))
            continue
        hi, lo = d.get("high_f"), d.get("low_f")
        hi_s = f"High: {hi:.0f}\u00b0F ({mr.c_from_f(hi)}\u00b0C)" if hi is not None else "High: n/a"
        lo_s = f"Low: {lo:.0f}\u00b0F ({mr.c_from_f(lo)}\u00b0C)" if lo is not None else "Low: n/a"
        B.append(("multi", (d["label"], [hi_s, lo_s, f"Precipitation: {d.get('precip') or 'n/a'}"])))
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
        B.append(("pair", ("Now at Furnace Creek", line)))
    if not rep.daily_climate:
        B.append(("note", ("Unavailable.", SANS)))

    # --- Year to date -------------------------------------------------------
    ytd = rep.year_to_date or {}
    B.append(("rule", 1.4))
    B.append(("heading", ("Year to Date", f"({ytd.get('label', 'Furnace Creek')})")))
    if ytd and not ytd.get("note"):
        hi, lo = ytd.get("high_f"), ytd.get("low_f")
        segs = []
        if hi is not None:
            segs.append(f"High: {hi:.0f}\u00b0F ({mr.c_from_f(hi)}\u00b0C) on {short_date(ytd['high_date'])}")
        if lo is not None:
            segs.append(f"Low: {lo:.0f}\u00b0F ({mr.c_from_f(lo)}\u00b0C) on {short_date(ytd['low_date'])}")
        B.append(("multi", ("Temperatures", segs or ["n/a"])))
        year = rep.report_date.split()[-1]
        B.append(("multi", ("Precipitation", [
            f"Year {year}: {ytd.get('calendar_precip', 0):.2f} inches",
            f"{short_date(ytd.get('water_year_start'))} through today: "
            f"{ytd.get('water_year_precip', 0):.2f} inches"])))
        gap = ytd.get("missing_days") or 0
        if gap:
            B.append(("small", f"{gap} day{'s' if gap != 1 else ''} not reported this "
                               f"year, so the totals may run low."))
    else:
        B.append(("note", ("Unavailable.", SANS)))

    # --- Current road conditions: the two-column list -----------------------
    B.append(("rule", 1.4))
    B.append(("heading", ("Current Road Conditions", None)))
    note = mr.road_status_note(rep)
    if note:
        B.append(("note", (note, SANS)))
    B.append(("note", ("For more park road information visit "
                       "nps.gov/deva/planyourvisit/conditions.htm.", ITAL)))
    roads = rep.roads_paved + rep.roads_unpaved
    if roads:
        items = sorted((_road_item(it) for it in roads), key=lambda t: t[0].lower())
        B.append(("gap", 4))
        B.append(("twocol", items))
    else:
        B.append(("note", ("Road status unavailable.", SANS)))

    if "approach" in inc and rep.approach:
        B.append(("subheading", "Roads OUTSIDE Death Valley"))
        for label, status in fold_routes(rep.approach, names=False, prefix="CA "):
            B.append(("pair", (label, status)))

    if "sierra" in inc and rep.sierra:
        B.append(("subheading", "Sierra Nevada Roads"))
        for label, status in fold_routes(rep.sierra, names=False, prefix="CA "):
            B.append(("pair", (label, status)))

    # --- Campgrounds --------------------------------------------------------
    B.append(("rule", 1.4))
    B.append(("heading", ("Campgrounds", None)))
    if rep.campgrounds:
        rows = [(short_label(it["name"]), _item_value(it)) for it in rep.campgrounds]
        if "fold_open_campgrounds" in compact:
            bare = [label for label, value in rows if value == "OPEN."]
            rest = [(label, value) for label, value in rows if value != "OPEN."]
            if len(bare) > 1:
                B.append(("note", ("Open: " + ", ".join(bare) + ".", SANS)))
                rows = rest
        if rows:
            B.append(("twocol", rows))
    else:
        B.append(("note", ("Campground status unavailable.", SANS)))

    # --- Facilities ---------------------------------------------------------
    if "facilities" in inc and rep.facilities:
        B.append(("rule", 1.4))
        B.append(("heading", ("Facilities", None)))
        B.append(("twocol", [(it["name"], _item_value(it)) for it in rep.facilities]))

    return B


# ---------------------------------------------------------------------------
# Measuring and drawing the body
# ---------------------------------------------------------------------------

def layout(blocks, pt):
    """
    Measure at body size pt. Returns (height, ops); ops are (y_offset, draw)
    with y measured downward from the top of the body area. A row that would
    run off the right edge makes the layout report infinite height, which
    pushes the fit loop to a smaller size.
    """
    lead = pt * 1.25
    ops = []
    y = 0.0
    overflow = False

    def add(fn):
        ops.append((y, fn))

    for kind, payload in blocks:
        if kind == "gap":
            y += payload * (pt / 10.0)

        elif kind == "rule":
            y += pt * 0.75
            add(lambda c, yy, w=payload: (c.setLineWidth(w), c.setStrokeGray(0),
                                          c.line(M_X, yy, RIGHT, yy)))
            y += pt * 0.15

        elif kind == "heading":
            title, suffix = payload
            size = pt + 2.0
            y += size * 1.1
            def draw(c, yy, t=title, s=suffix, sz=size):
                c.setFillGray(0)
                c.setFont(BOLD, sz)
                c.drawString(M_X, yy, t)
                if s:
                    c.setFont(SANS, sz - 3)
                    c.drawString(M_X + sw(t, BOLD, sz) + 4, yy, s)
            add(draw)
            y += pt * 0.45

        elif kind == "subheading":
            size = pt + 1.0
            y += pt * 0.6 + size * 1.05
            add(lambda c, yy, t=payload, sz=size: (c.setFont(BOLD, sz), c.drawString(M_X, yy, t)))
            y += pt * 0.25

        elif kind == "note":
            text, font = payload
            for line in wrap(text, font, pt, RIGHT - (M_X + INDENT)):
                y += lead
                add(lambda c, yy, t=line, f=font: (c.setFont(f, pt),
                                                   c.drawString(M_X + INDENT, yy, t)))

        elif kind == "small":
            size = pt - 1.5
            y += size * 1.3
            add(lambda c, yy, t=payload, sz=size: (c.setFillGray(0.4), c.setFont(ITAL, sz),
                                                   c.drawString(VALUE_X, yy, t),
                                                   c.setFillGray(0)))

        elif kind == "pair":
            label, value = payload
            x_label = M_X + INDENT
            room = VALUE_X - x_label - 6
            value_lines = wrap(value, SANS, pt, RIGHT - VALUE_X)
            if not label or sw(label, SANS, pt) <= room:
                label_lines = [label]
                dotted = bool(label)
            else:
                label_lines = wrap(label, SANS, pt, room)
                dotted = False
            for i in range(max(len(label_lines), len(value_lines))):
                y += lead
                ll = label_lines[i] if i < len(label_lines) else None
                vl = value_lines[i] if i < len(value_lines) else None
                def draw(c, yy, ll=ll, vl=vl, first=(i == 0), dotted=dotted):
                    c.setFont(SANS, pt)
                    if ll:
                        c.drawString(x_label, yy, ll)
                    if first and dotted:
                        draw_dots(c, x_label + sw(ll, SANS, pt) + 2, VALUE_X - 3, yy, pt)
                    if vl:
                        c.setFont(SANS, pt)
                        c.drawString(VALUE_X, yy, vl)
                add(draw)
            y += pt * 0.1

        elif kind == "multi":
            label, segs = payload
            y += lead
            xs = [VALUE_X, COL2_X, COL3_X]
            # Place segments, nudging right if one runs into the next.
            placed, x_prev_end = [], None
            for i, seg in enumerate(segs):
                x = xs[min(i, len(xs) - 1)]
                if x_prev_end is not None:
                    x = max(x, x_prev_end + 12)
                end = x + sw(seg, SANS, pt)
                placed.append((x, end, seg))
                x_prev_end = end
            if placed and placed[-1][1] > RIGHT + 0.5:
                overflow = True
            def draw(c, yy, label=label, placed=placed):
                c.setFont(SANS, pt)
                x_label = M_X + INDENT
                if label:
                    c.drawString(x_label, yy, label)
                    draw_dots(c, x_label + sw(label, SANS, pt) + 2, placed[0][0] - 3, yy, pt)
                for i, (x, end, seg) in enumerate(placed):
                    c.setFont(SANS, pt)
                    c.drawString(x, yy, seg)
                    if i + 1 < len(placed):
                        draw_dots(c, end + 2, placed[i + 1][0] - 3, yy, pt)
            add(draw)
            y += pt * 0.1

        elif kind == "twocol":
            items = payload
            col_w = (RIGHT - (M_X + INDENT) - GUTTER) / 2
            x_cols = [M_X + INDENT, M_X + INDENT + col_w + GUTTER]
            val_w = col_w - LABEL2_W
            measured = []
            for label, value in items:
                lab = [label] if sw(label, SANS, pt) <= LABEL2_W - 6 else wrap(label, SANS, pt, LABEL2_W - 6)
                val = wrap(value, SANS, pt, val_w)
                rows = max(len(lab), len(val))
                measured.append((lab, val, rows * lead + pt * 0.2))
            # Split in reading order (down the left, then down the right) at
            # whichever point makes the two columns closest in height.
            heights = [m[2] for m in measured]
            total = sum(heights)
            best_k, best = 0, float("inf")
            run = 0.0
            for k in range(len(heights) + 1):
                taller = max(run, total - run)
                if taller < best:
                    best, best_k = taller, k
                if k < len(heights):
                    run += heights[k]
            cols = [measured[:best_k], measured[best_k:]]
            top = y
            for ci, col in enumerate(cols):
                yc = top
                x0 = x_cols[ci]
                for lab, val, h in col:
                    for r in range(max(len(lab), len(val))):
                        yc += lead
                        ll = lab[r] if r < len(lab) else None
                        vl = val[r] if r < len(val) else None
                        def draw(c, yy, ll=ll, vl=vl, x0=x0, first=(r == 0), single=(len(lab) == 1)):
                            c.setFont(SANS, pt)
                            if ll:
                                c.drawString(x0, yy, ll)
                            if first and single and ll:
                                draw_dots(c, x0 + sw(ll, SANS, pt) + 2, x0 + LABEL2_W - 3, yy, pt)
                            if vl:
                                c.setFont(SANS, pt)
                                c.drawString(x0 + LABEL2_W, yy, vl)
                        ops.append((yc, draw))
                    yc += pt * 0.2
            y = top + best

    return (float("inf") if overflow else y), ops


# ---------------------------------------------------------------------------
# Masthead and foot, fixed in size
# ---------------------------------------------------------------------------

def draw_masthead(c, rep):
    """Black bar, park name, edition and date, disclaimer, heavy rule."""
    top = PAGE_H - M_TOP
    bar_h = 0.14 * inch
    c.setFillGray(0)
    c.rect(M_X, top - bar_h, CONTENT_W, bar_h, stroke=0, fill=1)

    y = top - bar_h - 27
    c.setFont(BOLD, 25)
    c.drawString(M_X, y, "Death Valley National Park")

    y -= 23
    edition = mr.edition_label(datetime.fromisoformat(rep.generated)).title() + ":"
    c.setFont(BOLD, 16)
    c.drawString(M_X, y, edition)
    c.setFont(SANS, 16)
    c.drawString(M_X + sw(edition, BOLD, 16) + 6, y, rep.report_date.replace(",", "", 1))

    # Disclaimer, then the official sources as working links -- clickable in a
    # PDF viewer, and still legible as names on paper.
    first, _, rest = mr.DISCLAIMER.partition(". ")
    size = 7.5
    y -= 13
    c.setFillGray(0.35)
    c.setFont(ITAL, size)
    c.drawString(M_X, y, first + ".")
    y -= size * 1.35
    lead = rest.rstrip(".") + ": "
    c.drawString(M_X, y, lead)
    x = M_X + sw(lead, ITAL, size)
    names = list(mr.OFFICIAL)
    for i, name in enumerate(names):
        w = sw(name, ITAL, size)
        c.setFillGray(0.15)
        c.drawString(x, y, name)
        c.setLineWidth(0.4)
        c.setStrokeGray(0.35)
        c.line(x, y - 1.2, x + w, y - 1.2)
        c.linkURL(mr.OFFICIAL[name], (x, y - 2, x + w, y + size), relative=0, thickness=0)
        x += w
        sep = ", " if i < len(names) - 2 else (" and " if i == len(names) - 2 else ".")
        c.setFillGray(0.35)
        c.drawString(x, y, sep)
        x += sw(sep, ITAL, size)
    c.setFillGray(0)
    c.setStrokeGray(0)

    y -= 8
    c.setLineWidth(3)
    c.line(M_X, y, RIGHT, y)
    return y                                   # body starts below this


def draw_foot(c, rep):
    """The single italic line, as on the original, with sources and a rule."""
    printed = datetime.fromisoformat(rep.generated)
    when = mr.stamp(printed, "%A, %B %-d, %Y at %-I:%M %p", "%A, %B %d, %Y at %I:%M %p")
    when = when.replace("AM", "am").replace("PM", "pm")

    y_rule = M_BOT + 2
    c.setLineWidth(1.6)
    c.setStrokeGray(0)
    c.line(M_X, y_rule, RIGHT, y_rule)

    c.setFillGray(0)
    c.setFont(ITAL, 8.5)
    c.drawString(M_X, y_rule + 20, f"Printed {when} Pacific. "
                                   "Conditions are subject to change without notice.")
    c.setFillGray(0.4)
    c.setFont(SANS, 6.5)
    c.drawString(M_X, y_rule + 8, "Sources: National Park Service, National Weather "
                                  "Service (zone CAZ522), RCC-ACIS, Caltrans.")
    c.setFillGray(0)


# ---------------------------------------------------------------------------
# Fit and render
# ---------------------------------------------------------------------------

def render(rep, path, *, verbose=True):
    """Fit and draw. Returns the body point size actually used."""
    c = rl_canvas.Canvas(path, pagesize=letter)
    c.setTitle(f"Death Valley National Park - {rep.report_date}")
    c.setAuthor("Unofficial reconstruction")

    body_top = draw_masthead(c, rep) - 2
    avail = body_top - (M_BOT + FOOTER_H)

    include = set(DROPPABLE)
    compact: list[str] = []
    dropped = []
    chosen = None
    while chosen is None:
        blocks = build_blocks(rep, include, compact)
        pt = MAX_PT
        while pt >= MIN_PT - 1e-9:
            height, ops = layout(blocks, pt)
            if height <= avail:
                chosen = (pt, ops)
                break
            pt -= STEP
        if chosen:
            break
        pending = [k for k in COMPACTIONS if k not in compact]
        if pending:
            compact.append(pending[0])
            continue
        sheddable = [s for s in DROPPABLE if s in include]
        if not sheddable:
            height, ops = layout(blocks, MIN_PT)
            chosen = (MIN_PT, ops)
            if verbose:
                print("Warning: content exceeds one page even at minimum size; "
                      "the tail may be clipped.", file=sys.stderr)
            break
        include.discard(sheddable[0])
        dropped.append(sheddable[0])

    pt, ops = chosen
    for y_off, fn in ops:
        fn(c, body_top - y_off)

    draw_foot(c, rep)
    c.showPage()
    c.save()

    if dropped and verbose:
        print("To fit one page, omitted: "
              + ", ".join(SECTION_NAMES[d] for d in dropped), file=sys.stderr)
    return pt


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="Morning Report (PDF)",
        description="Build the Death Valley report as a one-page PDF.")
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
