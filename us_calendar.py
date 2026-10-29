"""미국 증시 휴장일·조기 폐장일과 월물 옵션 만기일 (NYSE 공지 기준, 2026~2027).
해가 바뀌기 전에 다음 해 날짜를 추가할 것: https://www.nyse.com/markets/hours-calendars
"""
from datetime import date, time, timedelta

HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
}
# 주식 13:00, ETF·지수 옵션 13:15 마감
EARLY_CLOSE = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}


def is_trading_day(d):
    return d.weekday() < 5 and d not in HOLIDAYS


def monthly_expiry(y, m):
    """월물 만기 = 셋째 금요일. 그날이 휴장이면 앞당긴다(2027-06-18 → 06-17)."""
    d = date(y, m, 1)
    d += timedelta(days=(4 - d.weekday()) % 7 + 14)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def is_monthly(d):
    return d == monthly_expiry(d.year, d.month)


def close_time(d):
    """옵션 마감 시각(ET). 이 뒤로는 시세가 바뀌지 않는다."""
    return time(13, 15) if d in EARLY_CLOSE else time(16, 15)


def is_fresh(ts_et, ref_et, day, max_lag=timedelta(minutes=10)):
    """CBOE 파일 시각(ts_et)이 그날 시세인지. 장중엔 기준 시각(ref_et)보다 10분 넘게 묵으면 묵은 것,
    마감 뒤 생성된 그날 파일은 값이 더 안 바뀌므로 정상으로 본다(9/23엔 전날 밤 파일이 하루 종일 나왔다)."""
    if ts_et.date() != day:
        return False
    return ts_et.time() >= close_time(day) or ref_et - ts_et <= max_lag
