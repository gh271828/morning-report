#!/usr/bin/env python3
"""
Morning Report
==============

Reconstructs the Death Valley National Park "Morning Report" -- the one-page
daily sheet the park published until 2015 -- from the sources that replaced it.

    $ python morning_report.py                      # plain text to stdout
    $ python morning_report.py --format html -o report.html
    $ python morning_report.py --json               # structured data
    $ python morning_report.py --list-stations      # find ACIS climate stations

Data sources
------------
  Forecast .......... NWS zone forecast for CAZ522 "Death Valley National Park"
                      (api.weather.gov, falling back to forecast.weather.gov).
                      This is the *same product* the old report quoted, which is
                      why the "...in the mountains...at Furnace Creek" phrasing
                      comes back intact.
  Current obs ....... NWS/RAWS station DEVC1 (Furnace Creek Visitor Center) via
                      forecast.weather.gov.
  24-hr & YTD ....... RCC-ACIS (data.rcc-acis.org), COOP station Death Valley
                      (USC00042319) and any others configured below.
  Sun times ......... computed locally (NOAA solar equations, no network).
  Park roads ........ nps.gov/deva Alerts & Conditions page.
  Campgrounds ....... same page. (The Developed Campgrounds page builds its
                      table in JavaScript and yields nothing to a plain fetch.)
  Sierra passes ..... Caltrans roads.dot.ca.gov.

Requires: requests, beautifulsoup4
    pip install requests beautifulsoup4

Every section degrades independently: a source that is down or has been
redesigned produces a "[unavailable]" line, not a traceback.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import math
import re
import sys
import textwrap
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    sys.exit("This script needs Python 3.9 or newer (for zoneinfo).")

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests beautifulsoup4")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency: pip install beautifulsoup4")


# ---------------------------------------------------------------------------
# Configuration -- everything worth editing lives here
# ---------------------------------------------------------------------------

PARK_TZ = ZoneInfo("America/Los_Angeles")

# Furnace Creek Visitor Center
LAT, LON = 36.4622, -116.8636

# NWS public forecast zone: "Death Valley National Park"
FORECAST_ZONE = "CAZ522"

# NWS observation station at the Furnace Creek Visitor Center
OBS_STATION = "DEVC1"

# Climate stations for the temperature/precipitation block.
#   sid  -- an ACIS station id (COOP, GHCN, ICAO...). None means "look it up by
#           name", which is slower but survives station-id churn.
# The original report's second station was Scotty's Castle, which stopped
# reporting after the 2015 flood and has never come back. Stovepipe Wells is
# the closest analogue; run --list-stations to see what else is live.
CLIMATE_STATIONS = [
    ("Furnace Creek", "USC00042319"),
    ("Stovepipe Wells", None),
]

# The station whose year-to-date numbers head the report.
YTD_STATION = "Furnace Creek"

# Precipitation "year" used by the park: July 1 through June 30.
WATER_YEAR_START = (7, 1)

NPS_CONDITIONS_URL = "https://www.nps.gov/deva/planyourvisit/conditions.htm"
NPS_CAMPGROUNDS_URL = "https://www.nps.gov/deva/planyourvisit/developed-campgrounds.htm"
NWS_ZONE_API = "https://api.weather.gov/zones/forecast/{zone}/forecast"
NWS_ZONE_HTML = "https://forecast.weather.gov/MapClick.php?zoneid={zone}"
ACIS_STNDATA = "https://data.rcc-acis.org/StnData"
ACIS_STNMETA = "https://data.rcc-acis.org/StnMeta"
CALTRANS_URL = "https://roads.dot.ca.gov/?roadnumber={road}"

# Sierra Nevada passes, as the old report listed them. CA-88 (Carson Pass) is
# what the park now points at in place of CA-89 (Monitor Pass).
SIERRA_PASSES = [
    ("120", "Tioga Pass"),
    ("108", "Sonora Pass"),
    ("4", "Ebbetts Pass"),
    ("88", "Carson Pass"),
    ("89", "Monitor Pass"),
]

USER_AGENT = "MorningReport/1.0 (+https://github.com/gh271828/morning-report)"

WIDTH = 78          # printed line width
LABEL_COL = 32      # column where the dot leaders stop and text begins


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

class SourceError(Exception):
    """A data source was unreachable or unparseable."""


def fetch(url: str, *, timeout: int = 25, params=None, as_json: bool = False,
          accept: str | None = None):
    """GET a URL, with the User-Agent that api.weather.gov insists on."""
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    try:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        raise SourceError(f"{url}: {e}") from e
    return r.json() if as_json else r.text


def clean(s: str) -> str:
    """Collapse whitespace and normalise the punctuation NPS pastes in."""
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u00a0", " ").replace("\u2013", "-").replace("\u2014", "-")
    s = re.sub(r"\s+", " ", s).strip()
    # NPS wraps phrases in links, so get_text() leaves stray spaces behind.
    s = re.sub(r"\s+([.,;:!?%])", r"\1", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    # NPS sometimes runs sentences together ("Panamint Valley).Vehicles").
    # Requiring a lower-case letter or bracket before the period keeps decimals
    # (0.32) and abbreviations safe.
    s = re.sub(r"([a-z\)])\.([A-Z])", r"\1. \2", s)
    return s


def sentence_case_places(text: str) -> str:
    """NWS zone text is all-lowercase for place names; fix the obvious ones."""
    for wrong, right in (
        ("furnace creek", "Furnace Creek"),
        ("stovepipe wells", "Stovepipe Wells"),
        ("panamint", "Panamint"),
        ("badwater", "Badwater"),
    ):
        text = re.sub(wrong, right, text, flags=re.IGNORECASE)
    return text


def leader(label: str, value: str, *, width: int = WIDTH,
           col: int = LABEL_COL, fill: str = ".") -> str:
    """'Today ......... Sunny.' with the continuation hanging under the text."""
    label = label.rstrip()
    indent = len(label) - len(label.lstrip())
    body = textwrap.wrap(value, width=width - col) or [""]

    if len(label) > col - 3:
        # Too long for the label column. If the value is short it still fits on
        # one line; otherwise give the label its own line and run the value
        # underneath, rather than shunting text off the right edge.
        if len(label) + 1 + len(value) <= width:
            return f"{label} {value}"
        out = [label]
        out += [" " * col + line for line in body]
        return "\n".join(out)

    prefix = label + " " + fill * (col - len(label) - 2) + " "
    out = [prefix + body[0]]
    out += [" " * col + line for line in body[1:]]
    return "\n".join(out)


def wrap_block(text: str, indent: int = 0, width: int = WIDTH) -> str:
    return textwrap.fill(text, width=width - indent,
                         initial_indent=" " * indent,
                         subsequent_indent=" " * indent)


def c_from_f(f: float) -> int:
    return round((f - 32) * 5 / 9)


def short_error(e: Exception, limit: int = 60) -> str:
    """The tail of a requests error, without the URL repeated twice."""
    msg = str(e)
    msg = re.sub(r"https?://\S+", "", msg).strip(" :")
    msg = re.sub(r"\s+", " ", msg)
    return (msg[:limit] + "...") if len(msg) > limit else (msg or "unavailable")


def stamp(dt: datetime, fmt_unix: str, fmt_win: str) -> str:
    """strftime with %-d / %-I, which Windows spells differently."""
    try:
        return dt.strftime(fmt_unix)
    except ValueError:
        return dt.strftime(fmt_win)


# ---------------------------------------------------------------------------
# Sunrise / sunset -- NOAA solar equations, computed rather than scraped
# ---------------------------------------------------------------------------

def _solar_event(day: date, lat: float, lon: float, rising: bool) -> datetime | None:
    """
    UTC datetime of sunrise (rising=True) or sunset, by the standard sunrise
    equation: mean solar time -> solar transit -> hour angle.
    """
    # Days since the J2000.0 epoch.
    n = day.toordinal() - date(2000, 1, 1).toordinal()

    lw = -lon                       # west longitude, positive
    jstar = n + lw / 360.0          # mean solar noon, in days from J2000

    M = math.radians((357.5291 + 0.98560028 * jstar) % 360)          # mean anomaly
    C = (1.9148 * math.sin(M) + 0.0200 * math.sin(2 * M)
         + 0.0003 * math.sin(3 * M))                                  # equation of centre
    lam = math.radians((math.degrees(M) + C + 180 + 102.9372) % 360)  # ecliptic longitude

    jtransit = (2451545.0 + jstar
                + 0.0053 * math.sin(M) - 0.0069 * math.sin(2 * lam))

    decl = math.asin(math.sin(lam) * math.sin(math.radians(23.4397)))
    # -0.833 deg accounts for refraction plus the solar disc's radius.
    cos_w = ((math.sin(math.radians(-0.833)) -
              math.sin(math.radians(lat)) * math.sin(decl))
             / (math.cos(math.radians(lat)) * math.cos(decl)))
    if not -1 <= cos_w <= 1:
        return None  # polar day or night -- never an issue at Furnace Creek
    w = math.degrees(math.acos(cos_w))

    jevent = jtransit + (-w if rising else w) / 360.0
    unix = (jevent - 2440587.5) * 86400.0
    return datetime.fromtimestamp(unix, tz=timezone.utc)


def sun_times(day: date, lat: float = LAT, lon: float = LON):
    """(sunrise, sunset) as local park time for the given date."""
    rise = _solar_event(day, lat, lon, True)
    set_ = _solar_event(day, lat, lon, False)
    return (rise.astimezone(PARK_TZ) if rise else None,
            set_.astimezone(PARK_TZ) if set_ else None)


def fmt_time(dt: datetime | None) -> str:
    """'7:29 pm', rounded to the nearest minute (strftime would truncate)."""
    if dt is None:
        return "n/a"
    dt = (dt + timedelta(seconds=30)).replace(second=0, microsecond=0)
    return dt.strftime("%I:%M %p").lstrip("0").lower()


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------

@dataclass
class ForecastPeriod:
    name: str
    text: str
    is_daytime: bool | None = None
    start: str | None = None


def forecast_from_api(zone: str = FORECAST_ZONE) -> list[ForecastPeriod]:
    data = fetch(NWS_ZONE_API.format(zone=zone), as_json=True,
                 accept="application/geo+json")
    periods = data.get("properties", {}).get("periods") or []
    if not periods:
        raise SourceError("zone forecast returned no periods")
    out = []
    for p in periods:
        text = clean(p.get("detailedForecast") or "")
        if not text:
            continue
        out.append(ForecastPeriod(
            name=clean(p.get("name") or ""),
            text=sentence_case_places(text),
            is_daytime=p.get("isDaytime"),
            start=p.get("startTime"),
        ))
    if not out:
        raise SourceError("zone forecast periods were empty")
    return out


def forecast_from_html(zone: str = FORECAST_ZONE) -> list[ForecastPeriod]:
    """Fallback: scrape the public zone page."""
    soup = BeautifulSoup(fetch(NWS_ZONE_HTML.format(zone=zone)), "html.parser")
    body = soup.find(id="detailed-forecast-body") or soup.find(id="detailed-forecast")
    out: list[ForecastPeriod] = []
    if body:
        labels = body.select(".forecast-label")
        texts = body.select(".forecast-text")
        for lab, txt in zip(labels, texts):
            out.append(ForecastPeriod(clean(lab.get_text()),
                                      sentence_case_places(clean(txt.get_text()))))
    if not out:
        raise SourceError("could not find the detailed forecast on the zone page")
    return out


def get_forecast(zone: str = FORECAST_ZONE) -> list[ForecastPeriod]:
    try:
        return forecast_from_api(zone)
    except SourceError:
        return forecast_from_html(zone)


def slot_forecast(periods: list[ForecastPeriod], now: datetime) -> dict:
    """
    Map NWS periods onto the old report's slots:
        Today / Tonight / Tomorrow / Tomorrow night / Extended

    The NWS names the first period "Today" or "This Afternoon" or "Tonight"
    depending on when you ask, so key off daylight rather than the label.
    """
    slots = {"Today": None, "Tonight": None, "Tomorrow": None,
             "Tomorrow night": None, "Extended": None}
    if not periods:
        return slots

    seq = list(periods)
    first = seq[0]

    # Is the first period a daytime one? Prefer the API flag; else read the name.
    def daytime(p: ForecastPeriod) -> bool:
        if p.is_daytime is not None:
            return bool(p.is_daytime)
        return "night" not in p.name.lower() and "tonight" not in p.name.lower()

    order = ["Today", "Tonight", "Tomorrow", "Tomorrow night"]
    if not daytime(first):
        # Run after sunset: the first period is tonight.
        order = ["Tonight", "Tomorrow", "Tomorrow night"]

    for slot, p in zip(order, seq):
        slots[slot] = p

    # Extended: the first multi-day period, or failing that the next one along.
    used = len(order)
    for p in seq[used:]:
        if "through" in p.name.lower() or "-" in p.name:
            slots["Extended"] = p
            break
    if slots["Extended"] is None and len(seq) > used:
        slots["Extended"] = seq[used]
    return slots


# ---------------------------------------------------------------------------
# Current conditions at Furnace Creek
# ---------------------------------------------------------------------------

@dataclass
class CurrentObs:
    station: str = OBS_STATION
    temp_f: float | None = None
    humidity: str | None = None
    wind: str | None = None
    updated: str | None = None


def get_current_obs(zone: str = FORECAST_ZONE) -> CurrentObs:
    """The current-conditions panel on the zone page reports station DEVC1."""
    soup = BeautifulSoup(fetch(NWS_ZONE_HTML.format(zone=zone)), "html.parser")
    obs = CurrentObs()
    panel = soup.find(id="current-conditions")
    if not panel:
        raise SourceError("no current-conditions panel on the zone page")

    temp = panel.select_one(".myforecast-current-lrg")
    if temp:
        m = re.search(r"(-?\d+)", temp.get_text())
        if m:
            obs.temp_f = float(m.group(1))

    for row in panel.select("#current_conditions_detail tr"):
        cells = [clean(td.get_text()) for td in row.find_all("td")]
        if len(cells) != 2:
            continue
        key, val = cells[0].rstrip(":").lower(), cells[1]
        if key == "humidity":
            obs.humidity = val
        elif key.startswith("wind"):
            obs.wind = val
        elif key.startswith("last update"):
            obs.updated = val
    return obs


# ---------------------------------------------------------------------------
# Climate data (RCC-ACIS)
# ---------------------------------------------------------------------------

def acis_lookup_sid(name: str) -> str | None:
    """Resolve a station name to an ACIS id, searching a box around the park."""
    params = {"params": json.dumps({
        "bbox": "-118.0,35.5,-116.0,37.5",
        "meta": ["name", "sids", "valid_daterange"],
        "elems": "maxt",
    })}
    try:
        data = fetch(ACIS_STNMETA, params=params, as_json=True)
    except SourceError:
        return None
    want = name.lower().replace("'", "")
    for st in data.get("meta", []):
        stname = (st.get("name") or "").lower().replace("'", "")
        if want in stname:
            for sid in st.get("sids", []):
                token = sid.split()[0]
                if token.startswith("US") or token.isdigit():
                    return token
    return None


def acis_daily(sid: str, start: date, end: date) -> list[tuple[str, str, str, str]]:
    """Daily [date, maxt, mint, pcpn] rows. 'M' = missing, 'T' = trace."""
    params = {"params": json.dumps({
        "sid": sid,
        "sdate": start.isoformat(),
        "edate": end.isoformat(),
        "elems": "maxt,mint,pcpn",
        "meta": ["name"],
    })}
    data = fetch(ACIS_STNDATA, params=params, as_json=True)
    rows = data.get("data") or []
    if not rows:
        raise SourceError(f"ACIS returned no data for {sid}")
    return rows


def _num(v: str) -> float | None:
    if v in ("M", "S", "", None):
        return None
    if v == "T":
        return 0.0
    try:
        return float(str(v).rstrip("A"))
    except ValueError:
        return None


@dataclass
class DayClimate:
    label: str
    obs_date: str | None = None
    high_f: float | None = None
    low_f: float | None = None
    precip: str | None = None
    note: str | None = None


@dataclass
class YearToDate:
    label: str
    high_f: float | None = None
    high_date: str | None = None
    low_f: float | None = None
    low_date: str | None = None
    calendar_precip: float | None = None
    water_year_precip: float | None = None
    water_year_start: str | None = None
    missing_days: int = 0        # days in the calendar year with no observation
    note: str | None = None


def get_climate(today: date):
    """One ACIS pull per station covers both the 24-hour and YTD blocks."""
    wy_year = today.year if (today.month, today.day) >= WATER_YEAR_START else today.year - 1
    wy_start = date(wy_year, *WATER_YEAR_START)
    start = min(wy_start, date(today.year, 1, 1))

    dailies: list[DayClimate] = []
    ytd: YearToDate | None = None

    for label, sid in CLIMATE_STATIONS:
        if sid is None:
            sid = acis_lookup_sid(label)
        if sid is None:
            dailies.append(DayClimate(label, note="no reporting station found"))
            continue
        try:
            rows = acis_daily(sid, start, today)
        except SourceError as e:
            dailies.append(DayClimate(label, note=short_error(e)))
            continue

        # Most recent day that actually reported something.
        latest = None
        for row in reversed(rows):
            if any(_num(v) is not None for v in row[1:]):
                latest = row
                break
        if latest:
            dailies.append(DayClimate(
                label,
                obs_date=latest[0],
                high_f=_num(latest[1]),
                low_f=_num(latest[2]),
                precip=("T" if latest[3] == "T" else
                        (f"{_num(latest[3]):.2f}" if _num(latest[3]) is not None else None)),
            ))
        else:
            dailies.append(DayClimate(label, note="no recent observations"))

        if label == YTD_STATION:
            ytd = YearToDate(label, water_year_start=wy_start.isoformat())
            cal_total = wy_total = 0.0
            for d, mx, mn, pc in rows:
                day = date.fromisoformat(d)
                v = _num(mx)
                if v is not None and day.year == today.year:
                    if ytd.high_f is None or v > ytd.high_f:
                        ytd.high_f, ytd.high_date = v, d
                v = _num(mn)
                if v is not None and day.year == today.year:
                    if ytd.low_f is None or v < ytd.low_f:
                        ytd.low_f, ytd.low_date = v, d
                v = _num(pc)
                if v is None:
                    # A day the station did not report. Counting it silently as
                    # zero is how a season's total ends up quietly wrong.
                    if day.year == today.year:
                        ytd.missing_days += 1
                else:
                    if day.year == today.year:
                        cal_total += v
                    if day >= wy_start:
                        wy_total += v
            ytd.calendar_precip = round(cal_total, 2)
            ytd.water_year_precip = round(wy_total, 2)

    if ytd is None:
        ytd = YearToDate(YTD_STATION, note="unavailable")
    return dailies, ytd


# ---------------------------------------------------------------------------
# NPS Alerts & Conditions -- roads, campgrounds, facilities
# ---------------------------------------------------------------------------

# Accordion headings on the page, in the order they appear.
NPS_SECTIONS = [
    "Paved Roads",
    "Unpaved/Backcountry Roads",
    "Roads Outside the Park",
    "Wildflower Status",
    "Facilities/Popular Locations",
    "Campgrounds",
    "Weather Conditions",
]

SUBHEADS = ("CLOSED", "CAUTION", "OPEN", "PARTIALLY OPEN", "DELAYS", "RESTRICTED")

# Where the article ends and the site furniture begins. Without this the last
# detected section runs on and absorbs the footer's nav links.
STOP_MARKERS = ("last updated:", "park footer", "contact info", "mailing address",
                "stay connected", "experience your america")


@dataclass
class ConditionItem:
    status: str | None
    name: str
    detail: str = ""


@dataclass
class NPSConditions:
    updated: str | None = None
    roads_paved: list[ConditionItem] = field(default_factory=list)
    roads_unpaved: list[ConditionItem] = field(default_factory=list)
    facilities: list[ConditionItem] = field(default_factory=list)
    campgrounds: list[ConditionItem] = field(default_factory=list)
    last_updated_page: str | None = None
    raw_sections: dict = field(default_factory=dict)


def _flatten(node) -> list[tuple[str, str, object]]:
    """
    Walk the DOM in document order, yielding (tag, text, element) for
    block-level leaves. A <p> or <li> is taken whole and its children skipped,
    so nested <strong> text is not emitted twice.
    """
    out: list[tuple[str, str, object]] = []
    take_whole = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "button", "caption"}

    def walk(el):
        for child in getattr(el, "children", []):
            name = getattr(child, "name", None)
            if name is None:
                continue
            if name in ("script", "style", "nav", "footer", "form"):
                continue
            if name in take_whole:
                txt = clean(child.get_text(" "))
                if txt:
                    out.append((name, txt, child))
                continue
            walk(child)

    walk(node)
    return out


def _split_item(text: str, el=None) -> ConditionItem:
    """
    'Darwin Falls: Road is completely gone...' -> name + detail.

    NPS bolds the place name, so when the element is available the leading
    <strong> is the authoritative split. That matters because the bold
    sometimes ends in a period rather than a colon, which no regex on the
    flattened text can tell from a sentence break.
    """
    text = clean(text)
    if el is not None:
        lead = el.find(["strong", "b"])
        if lead is not None:
            name = clean(lead.get_text(" ")).strip(" :.-")
            rest = clean(text[len(clean(lead.get_text(" "))):]) if text else ""
            rest = rest.lstrip(" :.-").strip()
            if 2 <= len(name) <= 90:
                return ConditionItem(None, name, rest)
    m = re.match(r"^(.{2,70}?)\s*[:\u2013-]\s+(.*)$", text)
    if m:
        return ConditionItem(None, m.group(1).strip(" :-"), m.group(2).strip())
    return ConditionItem(None, text, "")


def get_nps_conditions(url: str = NPS_CONDITIONS_URL) -> NPSConditions:
    soup = BeautifulSoup(fetch(url), "html.parser")
    main = soup.find(id="main") or soup.find("main") or soup.body
    if main is None:
        raise SourceError("could not find the main content block")

    cond = NPSConditions()
    page_text = clean(main.get_text(" "))

    m = re.search(r"Road Status\s*Updated\s*(.{5,60}?Time)", page_text, re.I)
    if m:
        cond.updated = clean(m.group(1))
    m = re.search(r"Last updated:\s*([A-Z][a-z]+ \d{1,2}, \d{4})", page_text)
    if m:
        cond.last_updated_page = m.group(1)

    blocks = _flatten(main)

    # Truncate at the site footer so trailing sections do not run on into it.
    for i, (_tag, txt, _el) in enumerate(blocks):
        low = txt.lower()
        if any(low.startswith(m) for m in STOP_MARKERS):
            blocks = blocks[:i]
            break

    # Index where each known section begins.
    starts: list[tuple[int, str]] = []
    for i, (_tag, txt, _el) in enumerate(blocks):
        for sec in NPS_SECTIONS:
            norm = txt.lower().replace("\u2019", "'")
            if norm.startswith(sec.lower()) and (i, sec) not in starts:
                starts.append((i, sec))
                break
    starts.sort()

    sections: dict[str, list[tuple[str, str]]] = {}
    for idx, (i, sec) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(blocks)
        sections.setdefault(sec, blocks[i:end])

    def parse(sec: str) -> list[ConditionItem]:
        items: list[ConditionItem] = []
        status = None
        for tag, txt, el in sections.get(sec, []):
            bare = txt.rstrip(":").strip().upper()
            if bare in SUBHEADS:
                status = bare
                continue
            # A heading line that carries the status inline, e.g. "Paved RoadsCLOSED:"
            m = re.search(r"\b(" + "|".join(SUBHEADS) + r")\s*:?\s*$", txt.upper())
            if m and len(txt) < 60:
                status = m.group(1)
                continue
            if tag != "li":
                continue
            item = _split_item(txt, el)
            item.status = status
            items.append(item)

        if items:
            return items

        # Fallback: NPS has restructured the page and the bullets are no longer
        # <li>. Keep whatever prose is in the section rather than dropping it.
        for tag, txt, el in sections.get(sec, [])[1:]:
            if len(txt) < 15 or txt.rstrip(":").strip().upper() in SUBHEADS:
                continue
            it = _split_item(txt, el)
            it.status = status
            items.append(it)
        return items

    cond.roads_paved = parse("Paved Roads")
    cond.roads_unpaved = parse("Unpaved/Backcountry Roads")
    cond.facilities = parse("Facilities/Popular Locations")
    cond.campgrounds = parse("Campgrounds")
    cond.raw_sections = {k: [t for _tag, t, _el in v] for k, v in sections.items()}
    return cond


# ---------------------------------------------------------------------------
# Caltrans -- Sierra Nevada passes
# ---------------------------------------------------------------------------

def get_pass_status(road: str) -> str:
    text = BeautifulSoup(fetch(CALTRANS_URL.format(road=road)), "html.parser") \
        .get_text("\n")
    text = re.sub(r"\n{2,}", "\n", text)
    lines = [clean(l) for l in text.split("\n")]
    try:
        i = next(i for i, l in enumerate(lines)
                 if re.fullmatch(rf"(SR|US|I)[- ]?{road}", l, re.I))
    except StopIteration:
        raise SourceError(f"no section for highway {road}")
    chunk = []
    for l in lines[i + 1:]:
        if re.fullmatch(r"(SR|US|I)[- ]?\d+", l, re.I) or l.lower().startswith("back to top"):
            break
        if l and not l.startswith("Copyright"):
            chunk.append(l)
    body = " ".join(chunk)
    # Drop the bracketed district headers, keep the substance.
    body = re.sub(r"\[[^\]]*\]", "", body)
    body = clean(body)
    return body or "no restrictions reported"


def summarize_pass(text: str) -> str:
    low = text.lower()
    if "closed" in low:
        return text
    if "no traffic restrictions" in low:
        return "Open. No traffic restrictions reported."
    return text


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------

@dataclass
class Report:
    generated: str
    report_date: str
    forecast: dict = field(default_factory=dict)
    sunset_today: str | None = None
    sunrise_tomorrow: str | None = None
    current: dict | None = None
    daily_climate: list = field(default_factory=list)
    year_to_date: dict | None = None
    roads_paved: list = field(default_factory=list)
    roads_unpaved: list = field(default_factory=list)
    campgrounds: list = field(default_factory=list)
    facilities: list = field(default_factory=list)
    sierra: list = field(default_factory=list)
    roads_updated: str | None = None
    raw_sections: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


def build_report(*, skip_sierra: bool = False) -> Report:
    now = datetime.now(PARK_TZ)
    today = now.date()
    rep = Report(
        generated=now.isoformat(timespec="minutes"),
        report_date=stamp(now, "%A, %B %-d, %Y", "%A, %B %d, %Y"),
    )

    # Forecast
    try:
        slots = slot_forecast(get_forecast(), now)
        rep.forecast = {k: (asdict(v) if v else None) for k, v in slots.items()}
    except SourceError as e:
        rep.errors.append(f"forecast: {short_error(e)}")

    # Sun
    _, set_today = sun_times(today)
    rise_tomorrow, _ = sun_times(today + timedelta(days=1))
    rep.sunset_today = fmt_time(set_today)
    rep.sunrise_tomorrow = fmt_time(rise_tomorrow)

    # Current obs
    try:
        rep.current = asdict(get_current_obs())
    except SourceError as e:
        rep.errors.append(f"current conditions: {short_error(e)}")

    # Climate
    try:
        dailies, ytd = get_climate(today)
        rep.daily_climate = [asdict(d) for d in dailies]
        rep.year_to_date = asdict(ytd)
    except SourceError as e:
        rep.errors.append(f"climate: {short_error(e)}")

    # Park roads / campgrounds
    try:
        cond = get_nps_conditions()
        rep.roads_paved = [asdict(i) for i in cond.roads_paved]
        rep.roads_unpaved = [asdict(i) for i in cond.roads_unpaved]
        rep.campgrounds = [asdict(i) for i in cond.campgrounds]
        rep.facilities = [asdict(i) for i in cond.facilities]
        rep.roads_updated = cond.updated
        rep.raw_sections = cond.raw_sections
    except SourceError as e:
        rep.errors.append(f"park conditions: {short_error(e)}")

    # Sierra passes
    if not skip_sierra:
        for road, name in SIERRA_PASSES:
            try:
                rep.sierra.append({
                    "road": f"Hwy {road}",
                    "name": name,
                    "status": summarize_pass(get_pass_status(road)),
                })
            except SourceError as e:
                rep.sierra.append({"road": f"Hwy {road}", "name": name,
                                   "status": "[unavailable]"})
                rep.errors.append(f"Hwy {road}: {short_error(e)}")
    return rep


# ---------------------------------------------------------------------------
# Rendering -- plain text
# ---------------------------------------------------------------------------

def render_text(rep: Report) -> str:
    L: list[str] = []
    L.append("Death Valley National Park".center(WIDTH).rstrip())
    L.append(f"Morning Report: {rep.report_date}".center(WIDTH).rstrip())
    L.append("")

    # --- Weather forecast ---------------------------------------------------
    L.append("Weather Forecast")
    L.append("")
    any_fc = False
    for slot in ("Today", "Tonight", "Tomorrow", "Tomorrow night", "Extended"):
        p = rep.forecast.get(slot)
        if not p:
            continue
        any_fc = True
        L.append(leader(slot, p["text"], fill="\u2026"))
        L.append("")
    if not any_fc:
        L.append(wrap_block("[forecast unavailable]", 2))
        L.append("")

    L.append(f"Sunset today: {rep.sunset_today} "
             f"\u2026\u2026\u2026 Sunrise tomorrow: {rep.sunrise_tomorrow}")
    L.append("")

    # --- Current conditions -------------------------------------------------
    cur = rep.current
    if cur and cur.get("temp_f") is not None:
        bits = [f"{cur['temp_f']:.0f}\u00b0F ({c_from_f(cur['temp_f'])}\u00b0C)"]
        if cur.get("wind"):
            bits.append(f"wind {cur['wind']}")
        if cur.get("humidity"):
            bits.append(f"humidity {cur['humidity']}")
        line = ", ".join(bits)
        if cur.get("updated"):
            line += f"  (as of {cur['updated']})"
        L.append(leader("Currently at Furnace Creek", line, fill="\u2026"))
        L.append("")

    # --- Temperatures & precipitation --------------------------------------
    L.append("Temperatures & Precipitation (last 24 hours)")
    if rep.daily_climate:
        for d in rep.daily_climate:
            if d.get("note"):
                L.append(leader(d["label"], f"[{d['note']}]", fill="\u2026"))
                continue
            hi = d.get("high_f")
            lo = d.get("low_f")
            hi_s = f"{hi:.0f}\u00b0F ({c_from_f(hi)}\u00b0C)" if hi is not None else "n/a"
            lo_s = f"{lo:.0f}\u00b0F ({c_from_f(lo)}\u00b0C)" if lo is not None else "n/a"
            pr = d.get("precip") or "n/a"
            val = f"High: {hi_s}   Low: {lo_s}   Precip: {pr}"
            if d.get("obs_date"):
                val += f"   ({d['obs_date']})"
            L.append(leader(d["label"], val, fill="\u2026"))
    else:
        L.append(wrap_block("[unavailable]", 2))
    L.append("")

    # --- Year to date -------------------------------------------------------
    ytd = rep.year_to_date
    L.append(f"Year to Date ({(ytd or {}).get('label', YTD_STATION)})")
    if ytd and not ytd.get("note"):
        hi, lo = ytd.get("high_f"), ytd.get("low_f")
        temps = []
        if hi is not None:
            temps.append(f"High: {hi:.0f}\u00b0F ({c_from_f(hi)}\u00b0C) on {ytd['high_date']}")
        if lo is not None:
            temps.append(f"Low: {lo:.0f}\u00b0F ({c_from_f(lo)}\u00b0C) on {ytd['low_date']}")
        L.append(leader("Temperatures", "   ".join(temps) or "n/a", fill="\u2026"))
        year = rep.report_date.split()[-1]
        gap = ytd.get("missing_days") or 0
        precip = (f"Year {year}: {ytd.get('calendar_precip', 0):.2f} inches   "
                  f"{ytd.get('water_year_start')} through today: "
                  f"{ytd.get('water_year_precip', 0):.2f} inches")
        if gap:
            precip += f"   ({gap} day{'s' if gap != 1 else ''} not reported)"
        L.append(leader("Precipitation", precip, fill="\u2026"))
    else:
        L.append(wrap_block("[unavailable]", 2))
    L.append("")

    # --- Road conditions ----------------------------------------------------
    L.append("Current Road Conditions")
    if rep.roads_updated:
        L.append(wrap_block(f"(park road status updated {rep.roads_updated})", 2))
    L.append("")
    if rep.roads_paved or rep.roads_unpaved:
        if rep.roads_paved:
            L.append("  Paved Roads")
            for it in rep.roads_paved:
                L.append(_cond_line(it))
            L.append("")
        if rep.roads_unpaved:
            L.append("  Unpaved & Backcountry Roads")
            for it in rep.roads_unpaved:
                L.append(_cond_line(it))
            L.append("")
    else:
        L.append(wrap_block("[unavailable]", 2))
        L.append("")
    L.append(wrap_block("For more park road information see the Alerts & Conditions "
                        "page: " + NPS_CONDITIONS_URL, 2))
    L.append("")

    # --- Sierra passes ------------------------------------------------------
    if rep.sierra:
        L.append("Sierra Nevada Roads")
        for s in rep.sierra:
            L.append(leader(f"  {s['road']} ({s['name']})", s["status"], fill="\u2026"))
        L.append("")

    # --- Campgrounds --------------------------------------------------------
    L.append("Campgrounds")
    if rep.campgrounds:
        for it in rep.campgrounds:
            L.append(_cond_line(it))
    else:
        L.append(wrap_block("[unavailable]", 2))
    L.append("")

    # --- Facilities ---------------------------------------------------------
    if rep.facilities:
        L.append("Facilities & Popular Locations")
        for it in rep.facilities:
            L.append(_cond_line(it))
        L.append("")

    # --- Footer -------------------------------------------------------------
    L.append("-" * WIDTH)
    gen = stamp(datetime.fromisoformat(rep.generated), "%-I:%M %p", "%I:%M %p")
    L.append(wrap_block(f"Weather and road conditions as of {gen.lower()} "
                        "Pacific. Subject to change without notice."))
    L.append(wrap_block("Sources: National Park Service, National Weather Service "
                        "(zone CAZ522), RCC-ACIS, Caltrans."))
    if rep.errors:
        L.append("")
        L.append("Notes:")
        for e in rep.errors:
            L.append(wrap_block(f"- {e}", 2))
    return "\n".join(L)


def _cond_line(item: dict) -> str:
    label = "    " + item["name"]
    status = item.get("status")
    detail = (item.get("detail") or "").strip()
    if detail:
        detail = detail[0].upper() + detail[1:]   # NPS often starts mid-sentence
    value = f"{status}. {detail}".strip() if status else detail
    return leader(label, value or "see park website", fill="\u2026")


# ---------------------------------------------------------------------------
# Rendering -- HTML (prints to one page)
# ---------------------------------------------------------------------------

HTML_CSS = """
@page { size: letter; margin: 0.6in; }
body { font: 10.5pt/1.35 "Helvetica Neue", Helvetica, Arial, sans-serif;
       max-width: 7.3in; margin: 0 auto; color: #1a1a1a; }
h1 { font-size: 15pt; text-align: center; margin: 0; letter-spacing: .02em; }
h2.date { font-size: 11.5pt; font-weight: 400; text-align: center;
          margin: 0 0 14px; }
h3 { font-size: 11pt; margin: 14px 0 5px; border-bottom: 1px solid #999;
     padding-bottom: 2px; }
h4 { font-size: 10pt; margin: 9px 0 3px; font-weight: 600; }
dl { margin: 0; display: grid; grid-template-columns: 11em 1fr; gap: 2px 10px; }
dt { font-weight: 600; }
dd { margin: 0; }
.sun { text-align: center; margin: 10px 0; font-weight: 600; }
.muted { color: #666; }
footer { margin-top: 16px; border-top: 1px solid #999; padding-top: 6px;
         font-size: 8.5pt; color: #555; }
.status { font-weight: 700; }
"""


def render_html(rep: Report) -> str:
    e = html_mod.escape

    def dl(pairs):
        if not pairs:
            return '<p class="muted">[unavailable]</p>'
        rows = "".join(f"<dt>{e(k)}</dt><dd>{v}</dd>" for k, v in pairs)
        return f"<dl>{rows}</dl>"

    def cond_pairs(items):
        out = []
        for it in items:
            status = it.get("status")
            raw = (it.get("detail") or "").strip()
            if raw:
                raw = raw[0].upper() + raw[1:]
            det = e(raw)
            val = (f'<span class="status">{e(status)}.</span> {det}'
                   if status else det or "see park website")
            out.append((it["name"], val))
        return out

    parts = [f"<h1>Death Valley National Park</h1>",
             f'<h2 class="date">Morning Report: {e(rep.report_date)}</h2>']

    parts.append("<h3>Weather Forecast</h3>")
    fc = [(slot, e(rep.forecast[slot]["text"]))
          for slot in ("Today", "Tonight", "Tomorrow", "Tomorrow night", "Extended")
          if rep.forecast.get(slot)]
    parts.append(dl(fc))
    parts.append(f'<p class="sun">Sunset today: {e(rep.sunset_today or "")} '
                 f'&nbsp;&middot;&nbsp; Sunrise tomorrow: {e(rep.sunrise_tomorrow or "")}</p>')

    parts.append("<h3>Temperatures &amp; Precipitation (last 24 hours)</h3>")
    rows = []
    for d in rep.daily_climate:
        if d.get("note"):
            rows.append((d["label"], f'<span class="muted">[{e(d["note"])}]</span>'))
            continue
        hi, lo = d.get("high_f"), d.get("low_f")
        hi_s = f"{hi:.0f}&deg;F ({c_from_f(hi)}&deg;C)" if hi is not None else "n/a"
        lo_s = f"{lo:.0f}&deg;F ({c_from_f(lo)}&deg;C)" if lo is not None else "n/a"
        rows.append((d["label"],
                     f"High: {hi_s} &nbsp; Low: {lo_s} &nbsp; "
                     f"Precip: {e(d.get('precip') or 'n/a')}"))
    parts.append(dl(rows))

    ytd = rep.year_to_date or {}
    parts.append(f"<h3>Year to Date ({e(ytd.get('label', YTD_STATION))})</h3>")
    if ytd and not ytd.get("note"):
        hi, lo = ytd.get("high_f"), ytd.get("low_f")
        t = []
        if hi is not None:
            t.append(f"High: {hi:.0f}&deg;F ({c_from_f(hi)}&deg;C) on {e(ytd['high_date'])}")
        if lo is not None:
            t.append(f"Low: {lo:.0f}&deg;F ({c_from_f(lo)}&deg;C) on {e(ytd['low_date'])}")
        parts.append(dl([
            ("Temperatures", " &nbsp; ".join(t) or "n/a"),
            ("Precipitation",
             f"Calendar year: {ytd.get('calendar_precip', 0):.2f} in &nbsp; "
             f"Since {e(str(ytd.get('water_year_start')))}: "
             f"{ytd.get('water_year_precip', 0):.2f} in"),
        ]))
    else:
        parts.append('<p class="muted">[unavailable]</p>')

    parts.append("<h3>Current Road Conditions</h3>")
    if rep.roads_updated:
        parts.append(f'<p class="muted">Park road status updated {e(rep.roads_updated)}</p>')
    if rep.roads_paved:
        parts.append("<h4>Paved Roads</h4>")
        parts.append(dl(cond_pairs(rep.roads_paved)))
    if rep.roads_unpaved:
        parts.append("<h4>Unpaved &amp; Backcountry Roads</h4>")
        parts.append(dl(cond_pairs(rep.roads_unpaved)))
    if not (rep.roads_paved or rep.roads_unpaved):
        parts.append('<p class="muted">[unavailable]</p>')

    if rep.sierra:
        parts.append("<h3>Sierra Nevada Roads</h3>")
        parts.append(dl([(f"{s['road']} ({s['name']})", e(s["status"]))
                         for s in rep.sierra]))

    parts.append("<h3>Campgrounds</h3>")
    parts.append(dl(cond_pairs(rep.campgrounds)))

    if rep.facilities:
        parts.append("<h3>Facilities &amp; Popular Locations</h3>")
        parts.append(dl(cond_pairs(rep.facilities)))

    parts.append("<footer>Weather and road conditions as of generation time; "
                 "subject to change without notice.<br>Sources: National Park "
                 "Service, National Weather Service (zone CAZ522), RCC-ACIS, "
                 "Caltrans.</footer>")

    return ("<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>Morning Report &mdash; {e(rep.report_date)}</title>"
            f"<style>{HTML_CSS}</style></head><body>"
            + "".join(parts) + "</body></html>")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def list_stations():
    params = {"params": json.dumps({
        "bbox": "-118.0,35.5,-116.0,37.5",
        "meta": ["name", "sids", "valid_daterange", "ll"],
        "elems": "maxt",
    })}
    try:
        data = fetch(ACIS_STNMETA, params=params, as_json=True)
    except SourceError as e:
        print(f"Could not reach ACIS: {e}", file=sys.stderr)
        return 1
    for st in sorted(data.get("meta", []), key=lambda s: s.get("name", "")):
        sids = ", ".join(s.split()[0] for s in st.get("sids", []))
        rng = st.get("valid_daterange") or [["", ""]]
        last = rng[0][1] if rng and rng[0] else ""
        print(f"{st.get('name', '?'):<38} {sids:<34} last: {last}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="Morning Report",
        description="Rebuild the Death Valley National Park Morning Report.")
    ap.add_argument("--format", choices=("text", "html", "json"), default="text")
    ap.add_argument("-o", "--output", help="write to a file instead of stdout")
    ap.add_argument("--no-sierra", action="store_true",
                    help="skip the Caltrans pass lookups (five extra requests)")
    ap.add_argument("--list-stations", action="store_true",
                    help="list ACIS climate stations near the park and exit")
    ap.add_argument("--debug", action="store_true",
                    help="dump the raw NPS page sections that were detected")
    args = ap.parse_args(argv)

    if args.list_stations:
        return list_stations()

    rep = build_report(skip_sierra=args.no_sierra)

    if args.debug:
        if not rep.raw_sections:
            print("No NPS sections detected -- the page structure has probably "
                  "changed, or the fetch failed.", file=sys.stderr)
        for name, lines in rep.raw_sections.items():
            print(f"\n=== {name} " + "=" * (60 - len(name)), file=sys.stderr)
            for l in lines:
                print("  " + l[:200], file=sys.stderr)
        print("", file=sys.stderr)

    if args.format == "json":
        out = json.dumps(asdict(rep), indent=2)
    elif args.format == "html":
        out = render_html(rep)
    else:
        out = render_text(rep)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
