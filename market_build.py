"""
Builds market.json for the Market view (market.html).

Runs a curated set of Chartink Atlas widget queries (from the public "Top Dashboards",
credited by name) plus a few of our own, and saves the results in one small file.
Runs after the close (with build_dashboard.py) and every 15 minutes during market hours.

Nothing here is a tested trading signal. Only the STR100 rules are backtested.
"""
import json
import os
import sys
import time
from datetime import datetime

from chartink import Chartink, IST

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market.json")

# ---------- market pulse (single numbers) ----------
PULSE = {
    "adv": ("Market Matrix", "select groupcount( {cash} 1 where latest close > 1 day ago close ) / groupcount( {cash} 1 ) * 100 as 'v' WHERE {cash} 1 = 1"),
    "sma20": ("Market Matrix", "select groupcount( {cash} 1 where latest close > latest sma( latest close , 20 ) ) / groupcount( {cash} 1 ) * 100 as 'v' WHERE {cash} 1 = 1"),
    "sma50": ("Market Matrix", "select groupcount( {cash} 1 where latest close > latest sma( latest close , 50 ) ) / groupcount( {cash} 1 ) * 100 as 'v' WHERE {cash} 1 = 1"),
    "sma200": ("Market Matrix", "select groupcount( {cash} 1 where latest close > latest sma( latest close , 200 ) ) / groupcount( {cash} 1 ) * 100 as 'v' WHERE {cash} 1 = 1"),
    "hilo": ("Market Matrix", "select groupcount( {cash} 1 where latest high = latest max( 252 , latest high ) ) as 'high', groupcount( {cash} 1 where latest low = latest min( 252 , latest low ) ) as 'low' WHERE {cash} 1 = 1"),
    "rsi": ("Market Matrix", "select groupcount( {cash} 1 where latest rsi( 14 ) < 30 ) as 'oversold', groupcount( {cash} 1 where latest rsi( 14 ) > 70 ) as 'overbought' WHERE {cash} 1 = 1"),
    "vwap": ("Market Matrix", "select groupcount( 1 where latest close > latest vwap ) as 'above', groupcount( 1 where latest close < latest vwap ) as 'below' WHERE {cash} 1 = 1"),
    "vol": ("Intraday Traders desk", "select groupsum( {cash} latest volume * latest close where latest close > 1 day ago close ) / 10000000 as 'up', groupsum( {cash} latest volume * latest close where latest close < 1 day ago close ) / 10000000 as 'down' WHERE {cash} 1 = 1"),
}
INDICES = "select Daily Close as 'ltp', Daily \"close - 1 candle ago close / 1 candle ago close * 100\" as 'chg' WHERE {109630} 1 = 1 GROUP BY symbol ORDER BY 1 desc"

# ---------- money flow by sector ----------
SECTORS = {
    "day": ("Intraday Traders desk", "select groupcount( {cash} 1 where latest close > 1 day ago close ) / groupcount( {cash} 1 ) * 100 as 'v' WHERE {cash} 1 = 1 GROUP BY sector ORDER BY 1 desc"),
    "chg": ("Market Matrix", "select groupavg( {cash} latest close - 1 day ago close / 1 day ago close * 100 ) as 'v' WHERE {cash} 1 = 1 GROUP BY sector ORDER BY 1 desc"),
    "m1": ("RandomTest (monthly view)", "select groupavg( {cash} latest close - 21 days ago close / 21 days ago close * 100 ) as 'v' WHERE {cash} 1 = 1 GROUP BY sector ORDER BY 1 desc"),
    "y1": ("Market Matrix", "select groupavg( {cash} Latest Close - 252 days ago Close / 252 days ago Close * 100 ) as 'v' WHERE {cash} 1 = 1 GROUP BY sector ORDER BY 1 desc"),
    "volx": ("Momentum gainers", "select groupavg( {cash} latest volume / latest sma( latest volume , 20 ) * 100 - 100 ) as 'v' WHERE {cash} 1 = 1 GROUP BY sector ORDER BY 1 desc"),
}

LIQ = "latest close > 40 and latest sma( latest volume , 20 ) * latest close > 10000000 and market cap > 100"
ORDER_COL = {"chg": 1, "ltp": 2, "rvol": 3, "fromhigh": 4}
SECTOR_INDICES = "select latest close as 'ltp', latest close - 1 day ago close / 1 day ago close * 100 as 'chg', latest close - 5 days ago close / 5 days ago close * 100 as 'w1', latest close - 21 days ago close / 21 days ago close * 100 as 'm1' WHERE {45603} 1 = 1 GROUP BY symbol ORDER BY 2 desc"
COLS = ("select latest close - 1 day ago close / 1 day ago close * 100 as 'chg', latest close as 'ltp', "
        "latest volume / 1 day ago sma( latest volume , 20 ) as 'rvol', "
        "latest close - 1 day ago max( 252 , latest high ) / 1 day ago max( 252 , latest high ) * 100 as 'fromhigh', "
        "Sector as 'sector' ")

# ---------- scans: (group, title, source, plain-English rule, where clause, order column) ----------
SCANS = [
    ("swing", "str_like", "52-week high breakouts", "Intraday Traders desk / Gautam",
     "New 52-week high today, up 3%+, volume above its 20-day average, short EMAs stacked up",
     f"{LIQ} and latest high = latest max( 260 , latest high ) and latest close > 1 day ago close * 1.03 and latest volume > latest sma( latest volume , 20 ) and latest ema( latest close , 5 ) > latest ema( latest close , 13 ) and latest ema( latest close , 13 ) > latest ema( latest close , 26 )",
     "rvol"),
    ("swing", "near_high", "Within 3% of 52-week high", "Intraday Traders desk",
     "Close within 3% below its 52-week high, in an uptrend (50 EMA above 200 EMA)",
     f"{LIQ} and latest close >= 1 day ago max( 252 , latest high ) * 0.97 and latest close < 1 day ago max( 252 , latest high ) and latest ema( latest close , 50 ) > latest ema( latest close , 200 )",
     "fromhigh"),
    ("swing", "bigvol_ath", "All-time-high zone, 2x volume", "RandomTest (BigVolATH)",
     "Within 3% of the highest price in the data, volume at least 2x normal, Rs 4 cr+ a day traded",
     "latest close > latest max( 2000 , latest high * 0.97 ) and market cap > 900 and latest sma( latest volume , 20 ) * latest close > 40000000 and latest volume > latest sma( latest volume , 20 ) * 2",
     "rvol"),
    ("swing", "power", "Power momentum", "RandomTest (HuntATH)",
     "Up 19%+ in a month and 79%+ in a year, closing at a new 52-week closing high",
     "latest close > 21 days ago close * 1.19 and market cap > 400 and latest close > 1 day ago max( 252 , 1 day ago close ) and latest close > 252 days ago close * 1.79",
     "rvol"),
    ("swing", "short_bo", "Short-term breakouts", "Vibhor Screener",
     "Best 5-day close is 5%+ above the 120-day closing high, on rising volume",
     f"{LIQ} and latest max( 5 , latest close ) > 6 days ago max( 120 , latest close ) * 1.05 and latest volume > latest sma( latest volume , 5 ) and latest close > 1 day ago close",
     "chg"),
    ("swing", "weekly_bo", "Weekly breakouts", "Vibhor Screener",
     "Weekly close breaks above the 120-day high, weekly volume above average, RSI above 50",
     f"{LIQ} and weekly max( 5 , weekly close ) > 6 days ago max( 120 , latest close ) * 1.04 and weekly close > 1 week ago close and weekly volume > weekly sma( weekly volume , 20 ) and latest rsi( 14 ) > 50",
     "chg"),
    ("swing", "monthly_bo", "Long-term (monthly) breakouts", "RandomTest (LTBO)",
     "Monthly high breaks a 2-year high after a long base, monthly RSI above 60",
     f"{LIQ} and monthly high > 1 month ago max( 24 , monthly high ) and 1 month ago min( 24 , monthly low ) > 1 month ago max( 24 , monthly high ) * 0.5 and monthly rsi( 14 ) > 60 and market cap > 100",
     "chg"),
    ("swing", "demand", "Bounce from oversold (demand zone)", "Telegram NIFTY (Bullish area)",
     "Yesterday RSI was below 30 (deeply sold), today closes above yesterday's high",
     f"{LIQ} and 1 day ago rsi( 14 ) < 30 and latest close > 1 day ago high",
     "chg"),
    ("swing", "engulf", "Bullish engulfing", "The Future University",
     "Today's green candle swallows yesterday's red candle",
     f"{LIQ} and 1 day ago close < 1 day ago open and latest close > 1 day ago open and latest open < 1 day ago close and latest high > 1 day ago high and latest low < 1 day ago low",
     "chg"),
    ("intraday", "gainers", "Top gainers", "Market Matrix",
     "Biggest % gainers today (liquid stocks)", f"{LIQ} and latest close > 1 day ago close", "chg"),
    ("intraday", "losers", "Top losers", "Market Matrix",
     "Biggest % losers today (liquid stocks)", f"{LIQ} and latest close < 1 day ago close", "-chg"),
    ("intraday", "bo15", "15-minute breakouts", "Nitin Agarwal-85",
     "15-min candle closes above the last 20 candles' high, with above-average volume",
     f"{LIQ} and [0] 15 minute close > [-1] 15 minute max( 20 , [0] 15 minute close ) and [0] 15 minute volume > [0] 15 minute sma( [0] 15 minute volume , 20 )",
     "chg"),
    ("intraday", "buying15", "Buying in the last 15 minutes", "Vibhor Screener",
     "Three rising 5-min candles in a row, each on above-average volume",
     f"{LIQ} and [0] 5 minute close > [-1] 5 minute close and [-1] 5 minute close > [-2] 5 minute close and [-2] 5 minute close > [-3] 5 minute close and [0] 5 minute volume > [-1] 5 minute sma( [0] 5 minute volume , 15 ) and [-1] 5 minute volume > [-2] 5 minute sma( [0] 5 minute volume , 15 )",
     "chg"),
    ("intraday", "dayhigh", "At day's high, volume rising", "Vibhor Screener",
     "Trading at today's high with today's volume above its 20-day average",
     f"{LIQ} and latest close >= latest high * 0.995 and latest close > 1 day ago close and latest volume > latest sma( latest volume , 20 )",
     "rvol"),
    ("intraday", "volsurge", "Volume surge", "Market Matrix (Stock opportunities)",
     "Volume today already 3x its 20-day average",
     f"{LIQ} and latest volume > latest sma( latest volume , 20 ) * 3", "rvol"),
]


def post(ck, query, limit=80):
    """limit = how many groups (stocks / sectors). size=1 = only the latest bar."""
    return ck._post("/widget/process", {"query": query, "widget_id": "-1", "use_live": "1", "limit": str(limit),
                                        "size": "1", "end_time": "-1", "timeframe": "Daily", "symbol": "",
                                        "scan_link": "null"})


def rows(j):
    """groupData -> [(name, {col: latest value})]"""
    out = []
    for g in j.get("groupData", []):
        vals = {}
        for part in g.get("results", []):
            for k, v in part.items():
                vals[k] = v[-1] if isinstance(v, list) and v else v
        out.append((g.get("name"), vals))
    return out


def num(x, d=2):
    try:
        x = float(x)
        return None if (x != x or abs(x) > 1e9) else round(x, d)
    except (TypeError, ValueError):
        return None


def build(ck):
    t0 = time.time()
    out = dict(updated=datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"), pulse={}, indices=[], sectors={}, scans=[],
               note="Scans from Chartink Atlas public dashboards. Not backtested; only STR100 is.")
    for k, (src, q) in PULSE.items():
        try:
            r = rows(post(ck, q, 1))
            out["pulse"][k] = {kk: num(vv, 1) for kk, vv in (r[0][1] if r else {}).items()}
        except Exception as e:  # noqa
            print("pulse fail", k, e, file=sys.stderr)
    try:
        out["indices"] = [dict(sym=n, ltp=num(v.get("ltp")), chg=num(v.get("chg"))) for n, v in rows(post(ck, INDICES, 60))]
    except Exception as e:  # noqa
        print("indices fail", e, file=sys.stderr)
    try:
        out["sector_indices"] = [dict(sym=n, ltp=num(v.get("ltp")), chg=num(v.get("chg")), w1=num(v.get("w1")), m1=num(v.get("m1")))
                                 for n, v in rows(post(ck, SECTOR_INDICES, 80))]
    except Exception as e:  # noqa
        print("sector indices fail", e, file=sys.stderr)
        out["sector_indices"] = []
    sec = {}
    for k, (src, q) in SECTORS.items():
        try:
            for n, v in rows(post(ck, q, 80)):
                sec.setdefault(n, {})[k] = num(v.get("v"), 1)
        except Exception as e:  # noqa
            print("sector fail", k, e, file=sys.stderr)
    out["sectors"] = [dict(name=n, **v) for n, v in sec.items() if n and n not in ("n/a", "*no-groups*", "indices")]
    for group, key, title, src, rule, where, order in SCANS:
        try:
            desc = not order.startswith("-")
            col = order.lstrip("-")
            q = f"{COLS} WHERE( {{cash}} ( {where} ) ) GROUP BY symbol ORDER BY {ORDER_COL[col]} {'desc' if desc else 'asc'}"
            j = post(ck, q, 100)
            total = (j.get("metaData") or [{}])[0].get("availableLimit")
            items = []
            for n, v in rows(j):
                items.append(dict(sym=n, chg=num(v.get("chg")), ltp=num(v.get("ltp")), rvol=num(v.get("rvol")),
                                  fromhigh=num(v.get("fromhigh"), 1), sector=v.get("sector")))
            items.sort(key=lambda x: (x[col] is None, -(x[col] or 0) if desc else (x[col] or 0)))
            out["scans"].append(dict(group=group, key=key, title=title, source=src, rule=rule, count=total if total is not None else len(items), items=items[:40]))
        except Exception as e:  # noqa
            print("scan fail", key, e, file=sys.stderr)
            out["scans"].append(dict(group=group, key=key, title=title, source=src, rule=rule, count=None, items=[], error=True))
    out["took"] = round(time.time() - t0, 1)
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {OUT} in {out['took']}s: {sum(1 for s in out['scans'] if not s.get('error'))}/{len(out['scans'])} scans ok")
    return out


if __name__ == "__main__":
    build(Chartink())
