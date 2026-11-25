#!/usr/bin/env python3
"""
PiDash - home dashboard server for Raspberry Pi Zero 2 W.

Serves the kiosk dashboard (/) and a small JSON API to the screen on this
Pi only (127.0.0.1 - nothing is reachable from the network). Standard
library only; python3-dateutil (repeating events) and python3-pil (shrinking
photos for the background slideshow) are used if present. The Pi has little
memory, so photos and videos are made smaller in a low-priority helper
process, and the calendar is only worked out again when it changed.
"""
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

try:
    from dateutil.rrule import rrulestr
except ImportError:  # recurrence expansion disabled without dateutil
    rrulestr = None

HAVE_PIL = importlib.util.find_spec("PIL") is not None  # without it photos are shown at full size

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
DATA_DIR = os.environ.get("PIDASH_DATA", os.path.join(APP_DIR, "data"))
STATE_FILE = os.path.join(DATA_DIR, "state.json")
BOOT_SETTINGS = os.environ.get("PIDASH_BOOT_SETTINGS", "/boot/firmware/dashboard-settings.txt")
PHOTO_DIR = os.environ.get("PIDASH_PHOTOS", "/boot/firmware/photos")
VIDEO_DIR = os.environ.get("PIDASH_VIDEOS", os.path.join(os.path.dirname(PHOTO_DIR), "videos"))
PHOTO_CACHE = os.path.join(DATA_DIR, "photo-cache-1280")
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
PHOTO_MAX = 1280  # longest side sent to the screen
VIDEO_CACHE = os.path.join(DATA_DIR, "video-cache")
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp", ".mts", ".m2ts", ".mpg", ".mpeg", ".wmv"}
VIDEO_MAX = 854  # longest side; the Pi's processor plays this size without its 3D chip
VIDEO_FPS = 15
VIDEO_WAIT = float(os.environ.get("PIDASH_VIDEO_WAIT", "90"))  # let the screen start first
VIDEO_OFF = os.environ.get("PIDASH_VIDEO_OFF", "/var/lib/pidash-watchdog/video-off")  # made by the watchdog
PORT = int(os.environ.get("PIDASH_PORT", "8080"))
USER_AGENT = "PiDash/1.0 (home dashboard)"

LAYOUTS = ["classic", "mirrored", "stacked", "focus", "bigclock"]
THEMES = ["dark", "light", "black", "warm"]
WIDGETS = ["clock", "weather", "agenda", "countdowns", "forecast"]
PAGES = ["home", "week", "month", "agenda", "weather"]

DEFAULT_STATE = {
    "version": 1,
    "settings": {
        "page": "home",
        "pages": PAGES,
        "layout": "stacked",
        "slots": ["clock", "weather", "agenda", "countdowns"],
        "theme": "dark",
        "accent": "#4fc3f7",
        "rotation": 0,
        "autocycle": 0,
        "photoSeconds": 30,
        "videoSeconds": 60,
        "calendarMinutes": 2,
        "nightDim": True,
        "nightStart": "22:00",
        "nightEnd": "06:30",
        "clock24": False,
        "showSeconds": False,
        "weekStart": 0,
        "units": "imperial",
        "location": {"name": "", "lat": None, "lon": None},
        "feeds": [],
        "agendaDays": 7,
        "fontScale": 1.0,
    },
    "countdowns": [],
    "command": {"id": 0, "action": None},
}

_lock = threading.RLock()
_changed = threading.Condition(_lock)
_state = None


# ---------------------------------------------------------------- state ----
def load_state():
    global _state
    os.makedirs(DATA_DIR, exist_ok=True)
    state = json.loads(json.dumps(DEFAULT_STATE))
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            state.update({k: v for k, v in saved.items() if k != "settings"})
            state["settings"].update(saved.get("settings", {}))
        except Exception as e:  # corrupted file: keep defaults, keep a backup
            print("state load failed:", e, file=sys.stderr)
            try:
                os.replace(STATE_FILE, STATE_FILE + ".bad")
            except OSError:
                pass
    s = state["settings"]
    s["page"] = "home"  # always start on the overview
    s["pages"] = [p for p in s["pages"] if p in PAGES] or list(PAGES)
    s["slots"] = [known_widget(w) for w in s["slots"]]
    # later than any number the screen has seen, also after a restart of this program
    state["version"] = max(int(state.get("version", 0)), int(time.time()))
    _state = state
    import_boot_settings()


def known_widget(w):  # older settings may still list the removed notes box
    return w if w in WIDGETS else "countdowns"


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def bump():
    with _changed:
        _state["version"] = int(_state.get("version", 0)) + 1
        save_state()
        _changed.notify_all()


_rev = {"cal": 0, "media": 0}


def notify(what):
    """Tell the screen right away that the calendar or the slideshow changed
    (nothing is written to the SD card)."""
    with _changed:
        _rev[what] += 1
        _state["version"] += 1
        _changed.notify_all()


def parse_kv_file(path):
    out = {}
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip().upper()] = v
    return out


def import_boot_settings():
    """Pull calendar/location/units from the boot-partition settings file
    whenever it changes, so the dashboard can be configured from a PC."""
    try:
        mtime = os.path.getmtime(BOOT_SETTINGS)
    except OSError:
        return
    with _lock:
        if _state.get("_bootMtime") == mtime:
            return
        try:
            kv = parse_kv_file(BOOT_SETTINGS)
        except Exception as e:
            print("boot settings read failed:", e, file=sys.stderr)
            return
        s = _state["settings"]
        feeds = []
        for i in range(1, 10):
            key = "COZI_URL" if i == 1 else "CALENDAR_URL_%d" % i
            url = kv.get(key, "").strip()
            if url:
                name = kv.get("CALENDAR_NAME_%d" % i, "Cozi" if i == 1 else "Calendar %d" % i)
                color = kv.get("CALENDAR_COLOR_%d" % i, ["#4fc3f7", "#ffb74d", "#81c784", "#f06292",
                                                         "#ba68c8", "#4db6ac", "#e57373", "#fff176", "#90a4ae"][i - 1])
                feeds.append({"name": name, "url": url.replace("webcal://", "https://"), "color": color})
        if feeds:
            s["feeds"] = feeds
        loc = kv.get("WEATHER_LOCATION", "").strip()
        if loc and loc != s["location"].get("name"):
            s["location"] = {"name": loc, "lat": None, "lon": None}
        units = kv.get("UNITS", "").lower()
        if units in ("imperial", "metric"):
            s["units"] = units
        if kv.get("CLOCK_24H", "").lower() in ("yes", "true", "1"):
            s["clock24"] = True
        try:
            rot = int(kv.get("ROTATION", "") or -1)
            if rot in (0, 90, 180, 270):
                s["rotation"] = rot
        except ValueError:
            pass
        if kv.get("THEME", "").lower() in THEMES:
            s["theme"] = kv["THEME"].lower()
        if kv.get("LAYOUT", "").lower() in LAYOUTS:
            s["layout"] = kv["LAYOUT"].lower()
        slots = [w.strip().lower() for w in kv.get("WIDGETS", "").split(",")]
        if len(slots) in (3, 4):
            s["slots"] = [known_widget(w) for w in slots] + ["countdowns"] * (4 - len(slots))
        for key, name in (("AUTO_CYCLE", "autocycle"), ("PHOTO_SECONDS", "photoSeconds")):
            v = kv.get(key, "")
            if v.isdigit():
                # 0 = off; anything else at least 5 s so the Pi can keep up
                s[name] = max(5, min(3600, int(v))) if int(v) else 0
        v = kv.get("VIDEO_SECONDS", "")
        if v.isdigit():
            s["videoSeconds"] = max(5, min(300, int(v))) if int(v) else 0
        v = kv.get("CALENDAR_MINUTES", "")
        if v.isdigit() and int(v):
            s["calendarMinutes"] = min(60, int(v))
        night = kv.get("NIGHT_DIM", "").lower().replace(" ", "")
        if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d-([01]?\d|2[0-3]):[0-5]\d", night):
            a, b = night.split("-")
            s.update(nightDim=True, nightStart=a.zfill(5), nightEnd=b.zfill(5))
        elif night in ("no", "off"):
            s["nightDim"] = False
        downs = []
        for i in range(1, 21):
            m = re.fullmatch(r"(\d{4}-\d\d-\d\d)\s+(.+)", kv.get("COUNTDOWN_%d" % i, "").strip())
            if m:
                downs.append({"id": "file%d" % i, "date": m.group(1), "title": m.group(2)[:80], "emoji": ""})
        # keep any countdowns that did not come from the settings file
        _state["countdowns"] = [c for c in _state["countdowns"]
                                if not str(c.get("id", "")).startswith("file")] + downs
        _state["_bootMtime"] = mtime
        bump()


# ---------------------------------------------------------------- http -----
def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


_cache = {}


def cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
        _cache[key] = (now, val)
        return val
    except Exception:
        if hit:  # serve stale data when offline, try again in a minute
            _cache[key] = (now - ttl + 60, hit[1])
            return hit[1]
        raise


# ------------------------------------------------------------ weather ------
def geocode(name):
    q = urllib.parse.urlencode({"name": name, "count": 5, "language": "en", "format": "json"})
    data = json.loads(http_get("https://geocoding-api.open-meteo.com/v1/search?" + q))
    results = data.get("results") or []
    if not results and "," in name:  # "Austin, TX" -> retry with just the city
        data = json.loads(http_get("https://geocoding-api.open-meteo.com/v1/search?" +
                                   urllib.parse.urlencode({"name": name.split(",")[0].strip(), "count": 10,
                                                           "language": "en", "format": "json"})))
        results = data.get("results") or []
        hint = name.split(",", 1)[1].strip().lower()
        pref = [r for r in results if hint in (" ".join(str(r.get(k, "")) for k in
                                                ("admin1", "country", "country_code"))).lower()
                or hint == str(r.get("admin1", ""))[:2].lower()]
        results = pref or results
    return [{"name": ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country_code")) if x),
             "lat": r["latitude"], "lon": r["longitude"]} for r in results]


def get_weather():
    with _lock:
        s = _state["settings"]
        loc = dict(s["location"])
        units = s["units"]
    if not loc.get("name") and loc.get("lat") is None:
        return {"error": "no-location"}
    if loc.get("lat") is None:
        found = geocode(loc["name"])
        if not found:
            return {"error": "location-not-found", "name": loc["name"]}
        with _lock:
            s["location"] = {"name": loc["name"], "lat": found[0]["lat"], "lon": found[0]["lon"],
                             "label": found[0]["name"]}
            bump()
        loc = s["location"]
    params = {
        "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": 7,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
                   "wind_speed_10m,is_day,precipitation",
        "hourly": "temperature_2m,weather_code,precipitation_probability,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "sunrise,sunset,uv_index_max",
    }
    if units == "imperial":
        params.update(temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch")
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    data = cached("wx:" + url, 540, lambda: json.loads(http_get(url)))
    data["locationName"] = loc.get("label") or loc.get("name")
    data["units"] = units
    return data


# ----------------------------------------------------------- calendar ------
def ics_unfold(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n[ \t]", "", text).split("\n")


def ics_unescape(v):
    return (v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\"))


def parse_prop(line):
    m = re.match(r"^([A-Za-z0-9-]+)((?:;[^:]*)?):(.*)$", line)
    if not m:
        return None, {}, ""
    name, rawparams, value = m.group(1).upper(), m.group(2), m.group(3)
    params = {}
    for p in re.findall(r';([^=;]+)=("[^"]*"|[^;]*)', rawparams):
        params[p[0].upper()] = p[1].strip('"')
    return name, params, value


def to_local(value, params):
    """Return (naive local datetime, is_all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value[:8], "%Y%m%d")
        return d, True
    utc = value.endswith("Z")
    dt = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None), False
    tzid = params.get("TZID")
    if tzid and ZoneInfo:
        try:
            return dt.replace(tzinfo=ZoneInfo(tzid)).astimezone().replace(tzinfo=None), False
        except Exception:
            pass
    return dt, False


def parse_duration(v):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def parse_ics(text, since=None):
    """Events in the file; single events that were over before `since` are left out."""
    events, cur, depth = [], None, 0
    for line in ics_unfold(text):
        if line == "BEGIN:VEVENT":
            cur, depth = {"exdates": []}, 0
            continue
        if cur is None:
            continue
        if line.startswith("BEGIN:"):
            depth += 1  # skip VALARM etc.
            continue
        if line.startswith("END:") and depth:
            depth -= 1
            continue
        if line == "END:VEVENT":
            over = cur.get("end") or (cur["start"] + timedelta(days=1) if "start" in cur and not cur.get("duration") else None)
            if not (since and over and over < since and not cur.get("rrule") and not cur.get("recurrenceId")):
                events.append(cur)
            cur = None
            continue
        if depth:
            continue
        name, params, value = parse_prop(line)
        if name == "SUMMARY":
            cur["title"] = ics_unescape(value)
        elif name == "UID":
            cur["uid"] = value
        elif name == "STATUS":
            cur["status"] = value.upper()
        elif name == "DTSTART":
            cur["start"], cur["allDay"] = to_local(value, params)
        elif name == "DTEND":
            cur["end"], _ = to_local(value, params)
        elif name == "DURATION":
            cur["duration"] = parse_duration(value)
        elif name == "RRULE":
            cur["rrule"] = value
        elif name == "EXDATE":
            for v in value.split(","):
                try:
                    cur["exdates"].append(to_local(v, params)[0])
                except ValueError:
                    pass
        elif name == "RECURRENCE-ID":
            cur["recurrenceId"], _ = to_local(value, params)
    return events


def fix_until(rule):
    def repl(m):
        v = m.group(1)
        try:
            if v.endswith("Z"):
                dt = datetime.strptime(v[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                return "UNTIL=" + dt.astimezone().replace(tzinfo=None).strftime("%Y%m%dT%H%M%S")
            if len(v) == 8:
                return "UNTIL=" + v + "T235959"
        except ValueError:
            pass
        return "UNTIL=" + v
    return re.sub(r"UNTIL=([0-9TZ]+)", repl, rule)


WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def simple_rrule(rule, start, win_end):
    """Fallback recurrence expander (used when python3-dateutil is missing).
    Handles DAILY/WEEKLY/MONTHLY/YEARLY with INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY."""
    import calendar as _cal
    p = dict(x.split("=", 1) for x in rule.split(";") if "=" in x)
    freq, interval = p.get("FREQ", ""), max(1, int(p.get("INTERVAL", "1") or 1))
    count = int(p["COUNT"]) if p.get("COUNT") else None
    until = None
    if p.get("UNTIL"):
        try:
            until = datetime.strptime(p["UNTIL"][:15], "%Y%m%dT%H%M%S")
        except ValueError:
            until = None
    byday = [d for d in p.get("BYDAY", "").split(",") if d]
    bymd = [int(d) for d in p.get("BYMONTHDAY", "").split(",") if d.lstrip("-").isdigit()]
    out, n = [], 0

    def emit(dt):
        nonlocal n
        if dt < start:
            return True
        if (until and dt > until) or (count is not None and n >= count):
            return False
        n += 1
        out.append(dt)
        return dt < win_end

    def at(y, m, d):
        return datetime(y, m, d, start.hour, start.minute, start.second)

    for k in range(3000):
        if freq == "DAILY":
            if not emit(start + timedelta(days=k * interval)):
                break
        elif freq == "WEEKLY":
            days = sorted({WEEKDAYS[d[-2:]] for d in byday if d[-2:] in WEEKDAYS}) or [start.weekday()]
            base = start - timedelta(days=start.weekday()) + timedelta(weeks=k * interval)
            if not all(emit(base + timedelta(days=d)) for d in days):
                break
        elif freq in ("MONTHLY", "YEARLY"):
            if freq == "MONTHLY":
                mi = start.month - 1 + k * interval
                y, m = start.year + mi // 12, mi % 12 + 1
            else:
                y, m = start.year + k * interval, start.month
            last = _cal.monthrange(y, m)[1]
            cands = []
            if bymd:
                cands = [d if d > 0 else last + d + 1 for d in bymd]
            elif byday and freq == "MONTHLY":
                for d in byday:
                    wd = WEEKDAYS.get(d[-2:])
                    if wd is None:
                        continue
                    allw = [x for x in range(1, last + 1) if datetime(y, m, x).weekday() == wd]
                    num = d[:-2]
                    if num.lstrip("+-").isdigit():
                        i = int(num)
                        if -len(allw) <= i <= len(allw) and i != 0:
                            cands.append(allw[i - 1] if i > 0 else allw[i])
                    else:
                        cands.extend(allw)
            else:
                cands = [start.day]
            if not all(emit(at(y, m, d)) for d in sorted(set(cands)) if 1 <= d <= last):
                break
        else:
            return [start]
    return out


def expand(events, win_start, win_end):
    overrides = {(e.get("uid"), e["recurrenceId"]) for e in events if e.get("recurrenceId") and "start" in e}
    out = []
    for e in events:
        if "start" not in e or e.get("status") == "CANCELLED":
            continue
        start = e["start"]
        if "end" in e:
            length = e["end"] - start
        elif e.get("duration") is not None:
            length = e["duration"]
        else:
            length = timedelta(days=1) if e.get("allDay") else timedelta(0)
        starts = [start]
        if e.get("rrule") and not e.get("recurrenceId"):
            try:
                lo = win_start - length - timedelta(days=1)
                if rrulestr:
                    starts = rrulestr(fix_until(e["rrule"]), dtstart=start).between(lo, win_end, inc=True)
                else:
                    starts = [d for d in simple_rrule(fix_until(e["rrule"]), start, win_end) if lo <= d <= win_end]
            except Exception as ex:
                print("rrule error", e.get("title"), ex, file=sys.stderr)
        ex = set(e["exdates"])
        exd = {d.date() for d in ex} if e.get("allDay") else set()
        for st in starts:
            if st in ex or (e.get("allDay") and st.date() in exd):
                continue
            if e.get("rrule") and (e.get("uid"), st) in overrides:
                continue
            en = st + length
            if en <= win_start and not (length == timedelta(0) and st >= win_start):
                continue
            if st >= win_end:
                continue
            out.append({"title": e.get("title", "(no title)"), "start": st.isoformat(),
                        "end": en.isoformat(), "allDay": bool(e.get("allDay"))})
    return out


_cal = {}  # url -> {"key", "events", "ok": when last read, "error"}
_cal_lock = threading.Lock()   # one check at a time
_cal_first = threading.Event()  # set once the first check is done
_cal_wake = threading.Event()


def calendar_window():
    today = datetime.combine(date.today(), datetime.min.time())
    # from the first square of the month page (or a week ago) to six weeks ahead
    return min(today - timedelta(days=7), today.replace(day=1) - timedelta(days=6)), today + timedelta(days=42), today


def feed_list():
    with _lock:
        feeds = [dict(f) for f in _state["settings"]["feeds"]]
    for f in feeds:
        f["url"] = f.get("url", "").strip().replace("webcal://", "https://")
    return [f for f in feeds if f["url"]]


def check_feed(url):
    """Download one calendar. Returns True if something on it changed."""
    old = _cal.get(url, {})
    raw = http_get(url)
    if b"BEGIN:VCALENDAR" not in raw:
        raise ValueError("the link did not return a calendar")
    ws, we, today = calendar_window()
    # DTSTAMP is the time of the download with some calendars, so it does not count
    key = (hashlib.sha1(re.sub(rb"(?m)^DTSTAMP[^\n]*\n", b"", raw)).hexdigest(), today)
    events = old.get("events")
    if old.get("key") != key:
        events = expand(parse_ics(raw.decode("utf-8", "replace"), ws), ws, we)
    changed = events != old.get("events") or bool(old.get("error"))
    _cal[url] = {"key": key, "events": events, "ok": time.time(), "mono": time.monotonic(), "error": None, "fails": 0}
    return changed


def refresh_calendar():
    """Check every calendar now. Returns how many seconds to wait until the next check."""
    changed, wait = False, _state["settings"].get("calendarMinutes", 2) * 60
    with _cal_lock:
        feeds = feed_list()
        for url in [u for u in _cal if u not in [f["url"] for f in feeds]]:
            del _cal[url]
        for f in feeds:
            url = f["url"]
            try:
                changed |= check_feed(url)
            except Exception as ex:
                old = _cal.setdefault(url, {"key": None, "events": None, "fails": 0})
                old["fails"] = old.get("fails", 0) + 1
                new = str(ex)[:200] or type(ex).__name__
                changed |= new != old.get("error")
                old["error"] = new
                if isinstance(ex, urllib.error.HTTPError):  # the site itself said no: don't hammer it
                    wait = max(wait, min(900, 60 * 2 ** old["fails"]))
                elif old["events"] is None:  # no Wi-Fi yet after switching on
                    wait = min(wait, 20)
        _cal_first.set()
    if changed:
        notify("cal")
    return wait


def calendar_loop():
    while True:
        try:
            wait = refresh_calendar()
        except Exception as ex:
            print("calendar check failed:", ex, file=sys.stderr, flush=True)
            wait = 60
        _cal_wake.wait(wait)
        _cal_wake.clear()


def calendar_checked():
    """When every calendar was last read successfully (None: not yet)."""
    oks = [_cal.get(f["url"], {}).get("ok") for f in feed_list()]
    return min(oks) if oks and all(oks) else None


def get_calendar():
    _cal_first.wait(25)
    feeds, result, errors = feed_list(), [], []
    for f in feeds:
        c = _cal.get(f["url"], {})
        for ev in c.get("events") or []:
            result.append(dict(ev, calendar=f.get("name", "Calendar"), color=f.get("color", "#4fc3f7")))
        # a check that fails now and then is not worth a warning on the screen
        if c.get("error") and (c.get("events") is None or time.monotonic() - c["mono"] > 900):
            errors.append({"calendar": f.get("name"), "error": c["error"]})
    result.sort(key=lambda e: (e["start"], not e["allDay"]))
    return {"events": result, "errors": errors, "feeds": len(feeds), "checked": calendar_checked(),
            "day": date.today().isoformat()}


# -------------------------------------------------------------- photos -----
_photos = {"at": 0.0, "map": {}, "videos": {}, "skipped": 0}
_photo_lock = threading.Lock()


def scan_photos(ttl=30):
    """Return {id: path} for the pictures in the photos and videos folders (and
    their subfolders). The videos in them are kept in _photos["videos"]."""
    with _lock:
        if time.time() - _photos["at"] < ttl:
            return _photos["map"]
        found, videos, skipped = {}, {}, 0
        for root, dirs, files in (x for top in (PHOTO_DIR, VIDEO_DIR) for x in os.walk(top)):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                ext = os.path.splitext(name)[1].lower()
                if name.startswith("."):
                    continue
                if ext not in PHOTO_EXT and ext not in VIDEO_EXT:
                    skipped += ext in (".heic", ".heif", ".avif", ".tif", ".tiff")
                    continue
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                key = "%s|%d|%d" % (path, int(st.st_mtime), st.st_size)
                (videos if ext in VIDEO_EXT else found)[hashlib.sha1(key.encode("utf-8", "surrogateescape")).hexdigest()[:16]] = path
        try:  # forget shrunken copies of pictures that were removed
            for f in os.listdir(PHOTO_CACHE):
                if re.fullmatch(r"[0-9a-f]{16}\.jpg", f) and f[:16] not in found:
                    os.remove(os.path.join(PHOTO_CACHE, f))
        except OSError:
            pass
        _photos.update(at=time.time(), map=found, videos=videos, skipped=skipped)
        return found


def photo_file(pid):
    """Path of a screen-sized copy of the picture (made once, then kept)."""
    src = scan_photos().get(pid)
    if not src:
        return None
    if not HAVE_PIL:
        return src
    out = os.path.join(PHOTO_CACHE, pid + ".jpg")
    with _photo_lock:  # one at a time: the Pi has little memory
        if not os.path.exists(out):
            if pid in _bad_photos:
                return None
            os.makedirs(PHOTO_CACHE, exist_ok=True)
            try:
                subprocess.run([sys.executable, os.path.abspath(__file__), "--shrink", src, out], check=True, timeout=180)
            except (subprocess.SubprocessError, OSError):
                _bad_photos.add(pid)  # unreadable or too big: don't try again until restart
                return None
    return out


_bad_photos = set()


def low_priority():
    try:
        os.nice(19)
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("900")  # if memory runs out, stop this helper, not the screen
    except (AttributeError, OSError):
        pass


def shrink_photo(src, out):
    """Runs on its own (low priority, limited memory) so a huge picture
    cannot slow down or crash the dashboard."""
    low_priority()
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (350 << 20, 350 << 20))
    except (ImportError, OSError, ValueError):
        pass
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 60_000_000
    with Image.open(src) as im:
        im.draft("RGB", (PHOTO_MAX, PHOTO_MAX))  # JPEG: decode at reduced size
        im.thumbnail((PHOTO_MAX, PHOTO_MAX))
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            flat = Image.new("RGB", im.size, (13, 17, 23))
            flat.paste(im, mask=im.getchannel("A"))
            im = flat
        im.convert("RGB").save(out + ".tmp", "JPEG", quality=82)
    os.replace(out + ".tmp", out)


# -------------------------------------------------------------- videos -----
# Phone videos are far too heavy for the Pi to play behind the calendar, so
# each one is made small once, in the background, and the small copy is kept
# on the Pi. The file on the card is never changed.
_video_busy = None  # id being made small now


def video_cap():
    with _lock:
        return int(_state["settings"].get("videoSeconds") or 0)


def video_path(vid, ext=None):
    return os.path.join(VIDEO_CACHE, "%s-%d.mp4" % (vid, video_cap()) if ext is None else vid + ext)


def video_lists():
    """(ready to play, still to be made small, can't be played)"""
    ready, pending, failed = [], [], []
    if not video_cap() or os.path.exists(VIDEO_OFF):
        return ready, pending, failed
    scan_photos()
    for vid in list(_photos["videos"]):
        (ready if os.path.exists(video_path(vid)) else failed if os.path.exists(video_path(vid, ".bad")) else pending).append(vid)
    return ready, pending, failed


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def find_ffmpeg():
    return shutil.which(os.environ.get("PIDASH_FFMPEG", "ffmpeg"))


def convert_next():
    """Make one waiting video small. Returns False when there is nothing to do."""
    global _video_busy
    if os.path.isdir(PHOTO_DIR) and os.path.isdir(VIDEO_CACHE):
        scan_photos()  # forget copies of videos that were removed, and half-made ones
        for f in os.listdir(VIDEO_CACHE):
            if f[:16] not in _photos["videos"] or f.endswith(".tmp") or (f.endswith(".mp4") and f != os.path.basename(video_path(f[:16]))):
                os.remove(os.path.join(VIDEO_CACHE, f))
    pending, ffmpeg = video_lists()[1], find_ffmpeg()
    if not pending or not ffmpeg:
        return False
    os.makedirs(VIDEO_CACHE, exist_ok=True)
    if shutil.disk_usage(VIDEO_CACHE).free < 300 << 20:
        print("no room to prepare videos", file=sys.stderr, flush=True)
        return False
    # smallest file first, so something plays soon
    vid = min(pending, key=lambda v: os.path.getsize(_photos["videos"][v]))
    src, out, tries = _photos["videos"][vid], video_path(vid), read_int(video_path(vid, ".try"))
    err = ""
    if tries >= 4:  # each time it was tried the Pi was switched off or locked up
        err = "did not finish in %d tries" % tries
    else:
        write_text(video_path(vid, ".try"), str(tries + 1))
        _video_busy = vid
        start = time.monotonic()
        try:
            r = subprocess.run([sys.executable, os.path.abspath(__file__), "--small-video", ffmpeg, src, out + ".tmp", str(video_cap())],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=3 * 3600)
            if r.returncode:
                err = r.stderr.decode("utf-8", "replace").strip()[-300:] or "converter stopped (%d)" % r.returncode
            elif not os.path.exists(out + ".tmp") or os.path.getsize(out + ".tmp") < 1000:
                err = "no picture in the file"
        except (subprocess.SubprocessError, OSError) as ex:
            err = str(ex)[:300]
        finally:
            _video_busy = None
        if not err:
            os.replace(out + ".tmp", out)
            os.remove(video_path(vid, ".try"))
            print("video ready: %s (%d s of work)" % (os.path.basename(src), time.monotonic() - start), flush=True)
    if err:
        print("video can't be used: %s: %s" % (os.path.basename(src), err), file=sys.stderr, flush=True)
        write_text(video_path(vid, ".bad"), err)
        if os.path.exists(out + ".tmp"):
            os.remove(out + ".tmp")
    notify("media")
    return True


def small_video(ffmpeg, src, out, cap):
    """Runs on its own: low priority, limited memory, two of the four processor cores."""
    low_priority()
    info = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-i", src], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, timeout=120).stderr.decode("utf-8", "replace")
    size = re.search(r"Video: .*?\b(\d{2,5})x(\d{2,5})\b", info)
    big = bool(size) and int(size.group(1)) * int(size.group(2)) > 2_300_000  # more than full HD
    m = re.search(r"displaymatrix: rotation of (-?\d+)", info)
    turn = {90: "transpose=2,", -90: "transpose=1,", 270: "transpose=1,", -270: "transpose=2,",
            180: "hflip,vflip,", -180: "hflip,vflip,"}.get(int(m.group(1)) if m else 0, "")
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_STACK, (2 << 20, resource.getrlimit(resource.RLIMIT_STACK)[1]))
        resource.setrlimit(resource.RLIMIT_DATA, (300 << 20, 300 << 20))
    except (ImportError, OSError, ValueError):
        pass
    # phone videos are stored sideways with a note to turn them; turning them
    # after they were made small takes far less memory
    vf = ("fps={f},scale=w='if(gt(iw,ih),min({n},iw),-2)':h='if(gt(iw,ih),-2,min({n},ih))':flags=bilinear:out_color_matrix=bt709:out_range=tv,"
          "scale=trunc(iw/2)*2:trunc(ih/2)*2,{t}setsar=1,format=yuv420p,"
          "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709").format(f=VIDEO_FPS, n=VIDEO_MAX, t=turn)
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", "1" if big else "2",
           "-skip_loop_filter", "all" if big else "noref"] + (["-display_rotation", "0"] if turn else []) + [
           "-t", cap, "-i", src, "-map", "0:V:0", "-an", "-sn", "-dn", "-map_metadata", "-1", "-filter_threads", "1", "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-tune", "fastdecode", "-profile:v", "main", "-bf", "0", "-refs", "1",
           "-g", str(VIDEO_FPS * 2), "-crf", "25", "-maxrate", "1500k", "-bufsize", "3000k", "-threads", "2",
           "-movflags", "+faststart", "-f", "mp4", out]
    ionice = shutil.which("ionice")
    if ionice:
        cmd = [ionice, "-c", "3"] + cmd
    if os.name == "posix":
        os.execv(cmd[0], cmd)
    sys.exit(subprocess.run(cmd).returncode)


def video_loop():
    time.sleep(VIDEO_WAIT)
    while True:
        try:
            busy = convert_next()
        except Exception as ex:
            print("video preparation failed:", ex, file=sys.stderr, flush=True)
            busy = False
        time.sleep(5 if busy else min(300, max(5, VIDEO_WAIT)))


# ------------------------------------------------------------ commands -----
def run_command(action, value=None):
    with _lock:
        cmd = _state["command"]
        _state["command"] = {"id": int(cmd.get("id", 0)) + 1, "action": action, "value": value}
        bump()


# -------------------------------------------------------------- GPIO -------
def start_gpio():
    """Optional push buttons (BCM pins, wired to GND):
    GPIO5 = previous page, GPIO6 = next page, GPIO13 = next layout, GPIO19 = rotate widgets,
    GPIO26 = check the calendar now."""
    try:
        kv = parse_kv_file(BOOT_SETTINGS)
    except Exception:
        return
    if kv.get("GPIO_BUTTONS", "no").lower() not in ("yes", "true", "1"):
        return
    try:
        from gpiozero import Button
    except Exception as e:
        print("gpiozero unavailable:", e, file=sys.stderr)
        return
    mapping = {5: "prev", 6: "next", 13: "layout", 19: "swap", 26: "refresh"}
    global _buttons
    _buttons = []
    for pin, action in mapping.items():
        try:
            b = Button(pin, bounce_time=0.08)
            b.when_pressed = (lambda a=action: run_command(a))
            _buttons.append(b)
        except Exception as e:
            print("GPIO", pin, e, file=sys.stderr)


_alive = 0.0  # when the screen last reported that it is drawing
_screen = {}  # what it reported


# ------------------------------------------------------------- handler -----
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
        ".gif": "image/gif", ".bmp": "image/bmp", ".mp4": "video/mp4",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".json": "application/json", ".webmanifest": "application/manifest+json"}


def stats_line():
    v = _screen.get("video") or {}
    out = []
    if v:
        out.append("video playing (%s frames, %s skipped)" % (v.get("shown"), v.get("dropped")))
    if _screen.get("badVideos"):
        out.append("%s videos would not play" % _screen["badVideos"])
    if _video_busy:
        out.append("preparing a video")
    ok = calendar_checked()
    out.append("calendar read %d s ago" % (time.time() - ok) if ok else "calendar not read yet")
    return ", ".join(out)


class Handler(BaseHTTPRequestHandler):
    server_version = "PiDash/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("PIDASH_VERBOSE"):
            super().log_message(fmt, *args)

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def serve_static(self, rel):
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
            return self.send_error(404)
        return self.send_file(path)

    def send_file(self, path):
        """Sent in small pieces (a video must not be held in memory), and from
        any place in the file, as the video player asks for."""
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            start, end = 0, size - 1
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            part = bool(m and (m.group(1) or m.group(2)))
            if part:
                if m.group(1):
                    start, end = int(m.group(1)), min(int(m.group(2) or size), size - 1)
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            self.send_response(206 if part else 200)
            self.send_header("Content-Type", MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream"))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Accept-Ranges", "bytes")
            if part:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(left, 65536))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (ConnectionError, TimeoutError):
                self.close_connection = True  # the player moved on

    def public_state(self):
        with _lock:
            return dict({k: v for k, v in _state.items() if not k.startswith("_") and k != "notes"}, rev=dict(_rev))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self.serve_static("index.html")
            if p == "/api/photos":
                ready, pending, failed = video_lists()
                return self.send_json({"photos": list(scan_photos()), "skipped": _photos["skipped"],
                                       "videos": ready, "pending": len(pending), "failed": len(failed)})
            if p.startswith("/video/"):
                vid = p[len("/video/"):]
                scan_photos()
                path = video_path(vid) if vid in _photos["videos"] else ""
                return self.send_file(path) if os.path.isfile(path) else self.send_error(404)
            if p.startswith("/photo/"):
                path = photo_file(p[len("/photo/"):])
                return self.send_file(path) if path else self.send_error(404)
            if p.startswith("/static/"):
                return self.serve_static(p[len("/static/"):])
            if p == "/api/state":
                import_boot_settings()
                since = int((q.get("since") or ["0"])[0])
                if since:  # wait (asleep, no CPU) until something changes
                    with _changed:
                        _changed.wait_for(lambda: _state["version"] != since, timeout=25)
                return self.send_json(self.public_state())
            if p == "/api/calendar":
                return self.send_json(get_calendar())
            if p == "/api/weather":
                return self.send_json(get_weather())
            if p == "/api/info":
                return self.send_json({"ok": True, "photoTools": HAVE_PIL})
            if p == "/api/stats":  # one line for the watchdog's log
                return self.send_json(stats_line())
            if p == "/api/health":  # seconds since the screen last reported in (-1: not yet)
                return self.send_json(int(time.monotonic() - _alive) if _alive else -1)
            return self.send_error(404)
        except Exception as ex:
            return self.send_json({"error": str(ex)[:300]}, 500)

    def do_POST(self):
        global _alive
        p = urllib.parse.urlparse(self.path).path
        try:
            body = self.read_json()
            if p == "/api/alive":
                _alive = time.monotonic()
                _screen.clear()
                _screen.update(body)
                return self.send_json({"checked": calendar_checked(), "busy": bool(_video_busy)})
            if p == "/api/refresh":
                _cache.clear()
                refresh_calendar()
                return self.send_json({"ok": not any(c.get("error") for c in _cal.values()), "checked": calendar_checked()})
            with _lock:
                if p == "/api/settings":
                    s = _state["settings"]
                    for k, v in body.items():
                        if k in DEFAULT_STATE["settings"]:
                            s[k] = v
                    if "location" in body and body["location"].get("lat") is None:
                        s["location"] = {"name": body["location"].get("name", ""), "lat": None, "lon": None}
                    bump()
                    return self.send_json(self.public_state())
            return self.send_error(404)
        except Exception as ex:
            return self.send_json({"error": str(ex)[:300]}, 400)


def main():
    if sys.argv[1:2] == ["--shrink"]:
        return shrink_photo(*sys.argv[2:4])
    if sys.argv[1:2] == ["--small-video"]:
        return small_video(*sys.argv[2:6])
    load_state()
    start_gpio()
    for fn in (calendar_loop, video_loop):
        threading.Thread(target=fn, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print("PiDash listening on http://127.0.0.1:%d  photos: %s" % (PORT, PHOTO_DIR), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
