"""High-impact macro event calendar (FOMC, US CPI, US NFP).
Source of truth: data/events.json (hand-maintained, each entry carries its source URL).
`python3 events.py --refresh` re-reads the Federal Reserve FOMC calendar page and adds any new
FOMC statement dates (BLS pages block scripted downloads, so CPI/NFP are maintained by hand)."""
import datetime as dt, html, json, os, re, sys
from zoneinfo import ZoneInfo
import requests
from okxbot import config

EVENTS_F = os.path.join(config.DATA_DIR, "events.json")
ET, LOCAL = ZoneInfo("America/New_York"), ZoneInfo(config.TIMEZONE)
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"


def load():
    if not os.path.exists(EVENTS_F):
        return []
    evs = json.load(open(EVENTS_F))["events"]
    for e in evs:
        e["dt"] = dt.datetime.fromisoformat(e["time_et"]).replace(tzinfo=ET)
    return sorted(evs, key=lambda e: e["dt"])


def upcoming(hours=24 * 60, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    return [e for e in load() if -1 <= (e["dt"] - now).total_seconds() / 3600 <= hours]


def risk_window(now=None):
    """('block', ev) within EVENT_BLOCK_BEFORE_H before .. EVENT_BLOCK_AFTER_H after an event;
    ('halve', ev) within EVENT_HALVE_BEFORE_H before; else (None, None)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    best = (None, None)
    for e in load():
        h = (e["dt"] - now).total_seconds() / 3600
        if -config.EVENT_BLOCK_AFTER_H <= h <= config.EVENT_BLOCK_BEFORE_H:
            return "block", e
        if 0 < h <= config.EVENT_HALVE_BEFORE_H and best[0] is None:
            best = ("halve", e)
    return best


def fmt(e):
    loc = e["dt"].astimezone(LOCAL)
    return f"{e['name']} @ {loc:%Y-%m-%d %H:%M} UTC+8 ({e['dt']:%b %d %H:%M} ET) [src: {e['source']}]"


def refresh_fomc():
    t = requests.get(FED_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).text
    year = dt.date.today().year
    out = []
    for y in (year, year + 1):
        i = t.find(f"{y} FOMC Meetings")
        if i < 0:
            continue
        j = t.find(f"{y - 1} FOMC Meetings", i)
        seg = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t[i:j if j > 0 else i + 40000])))
        seg = seg.split("Meeting associated with")[0]   # drop footnotes (e.g. "next meeting ... 2028")
        for m in re.finditer(r"(January|February|March|April|May|June|July|August|September|October|November|December)"
                             r"(?:/(\w+))? (\d{1,2})-(\d{1,2})(\*?)", seg):
            month = m.group(2) or m.group(1)
            d = dt.datetime.strptime(f"{month} {m.group(4)} {y}", "%B %d %Y")
            out.append({"name": "FOMC rate decision (statement 14:00 ET)" + (" + SEP/dot plot" if m.group(5) else ""),
                        "kind": "FOMC", "time_et": d.replace(hour=14).isoformat(), "source": FED_URL})
    data = json.load(open(EVENTS_F)) if os.path.exists(EVENTS_F) else {"events": []}
    have = {(e["kind"], e["time_et"]) for e in data["events"]}
    added = [e for e in out if (e["kind"], e["time_et"]) not in have]
    data["events"] += added
    data["updated"] = dt.datetime.now(LOCAL).isoformat(timespec="seconds")
    json.dump(data, open(EVENTS_F, "w"), indent=1)
    return added


if __name__ == "__main__":
    if "--refresh" in sys.argv:
        print("added FOMC entries:", refresh_fomc())
    for e in upcoming(24 * 60):
        print(fmt(e))
