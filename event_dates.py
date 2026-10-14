"""Historical high-impact US macro release times (UTC), 2023-06 .. today.
Sources:
  CPI release dates: BLS archive https://www.bls.gov/bls/news-release/cpi.htm (PDF names cpi_MMDDYYYY.pdf), 08:30 ET
  NFP (Employment Situation): https://www.bls.gov/bls/news-release/empsit.htm (empsit_MMDDYYYY.pdf), 08:30 ET
  FOMC statements: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (monetaryYYYYMMDDa.htm), 14:00 ET
  (2025-08-22 excluded: not a rate decision; Oct-2025 CPI/NFP not published due to the shutdown.)
"""
import datetime as dt
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
CPI = "2023-06-13 2023-07-12 2023-08-10 2023-09-13 2023-10-12 2023-11-14 2023-12-12 2024-01-11 2024-02-13 2024-03-12 2024-04-10 2024-05-15 2024-06-12 2024-07-11 2024-08-14 2024-09-11 2024-10-10 2024-11-13 2024-12-11 2025-01-15 2025-02-12 2025-03-12 2025-04-10 2025-05-13 2025-06-11 2025-07-15 2025-08-12 2025-09-11 2025-10-24 2025-12-18 2026-01-13 2026-02-13 2026-03-11 2026-04-10 2026-05-12 2026-06-10 2026-07-14 2026-08-12 2026-09-11".split()
NFP = "2023-06-02 2023-07-07 2023-08-04 2023-09-01 2023-10-06 2023-11-03 2023-12-08 2024-01-05 2024-02-02 2024-03-08 2024-04-05 2024-05-03 2024-06-07 2024-07-05 2024-08-02 2024-09-06 2024-10-04 2024-11-01 2024-12-06 2025-01-10 2025-02-07 2025-03-07 2025-04-04 2025-05-02 2025-06-06 2025-07-03 2025-08-01 2025-09-05 2025-11-20 2025-12-16 2026-01-09 2026-02-11 2026-03-06 2026-04-03 2026-05-08 2026-06-05 2026-07-02 2026-08-07 2026-09-04 2026-10-02".split()
FOMC = "2023-06-14 2023-07-26 2023-09-20 2023-11-01 2023-12-13 2024-01-31 2024-03-20 2024-05-01 2024-06-12 2024-07-31 2024-09-18 2024-11-07 2024-12-18 2025-01-29 2025-03-19 2025-05-07 2025-06-18 2025-07-30 2025-09-17 2025-10-29 2025-12-10 2026-01-28 2026-03-18 2026-04-29 2026-06-17 2026-07-29 2026-09-16".split()

def events(until=None):
    until = until or dt.datetime.now(dt.timezone.utc)
    out = []
    for kind, dates, hm in (("CPI", CPI, (8, 30)), ("NFP", NFP, (8, 30)), ("FOMC", FOMC, (14, 0))):
        for d in dates:
            y, m, dd = map(int, d.split("-"))
            t = dt.datetime(y, m, dd, *hm, tzinfo=ET).astimezone(dt.timezone.utc)
            if t < until - dt.timedelta(hours=14):
                out.append((t, kind))
    return sorted(out)
