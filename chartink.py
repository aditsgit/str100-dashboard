"""Small Chartink client. Uses the same public endpoints the Chartink website uses.
For personal use only."""
import re
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://chartink.com"
IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Your two screeners, written as Chartink scan clauses
STR_CLAUSE = (
    "( {cash} ( 1 day ago close > 126 days ago close * 2 and "
    "latest ema( latest close , 50 ) > latest ema( latest close , 200 ) and "
    "1 day ago close > ( 1 day ago max( 250 , latest high ) ) * 0.9 and "
    "1 day ago max( 10 , latest high ) < latest close and "
    "latest close > 1 day ago close * 1.02 and latest close > 40 and "
    "latest sma( latest volume , 20 ) * 1 day ago close > 10000000 and "
    "market cap > 100 ) )"
)
HEALTH_CLAUSE = (
    "( {cash} ( latest close > latest ema( latest close , 50 ) and latest close > 40 and "
    "latest sma( latest volume , 20 ) * latest close > 10000000 ) )"
)
UNIVERSE_CLAUSE = (
    "( {cash} ( latest close > 40 and latest sma( latest volume , 20 ) * latest close > 10000000 ) )"
)


def ist_date(ms):
    return datetime.fromtimestamp(ms / 1000, IST).strftime("%Y-%m-%d")


class Chartink:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.token = None

    def _login(self):
        r = self.s.get(f"{BASE}/screener/market-health-228", timeout=30)
        r.raise_for_status()
        m = re.search(r'name="csrf-token"\s+content="([^"]+)"', r.text)
        if not m:
            raise RuntimeError("Could not read Chartink csrf token (site layout changed?)")
        self.token = m.group(1)

    def _post(self, path, data):
        last = None
        for attempt in range(4):
            try:
                if not self.token:
                    self._login()
                r = self.s.post(
                    BASE + path, data=data, timeout=60,
                    headers={"x-csrf-token": self.token, "x-requested-with": "XMLHttpRequest",
                             "referer": f"{BASE}/screener/str100-leade1"},
                )
                if r.status_code in (419, 401, 403):
                    self.token = None
                    raise RuntimeError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r.json()
            except Exception as e:  # noqa
                last = e
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"Chartink request failed: {path}: {last}")

    # --- daily history of a scan: {date: [symbols]} -------------------------
    def backtest(self, clause):
        j = self._post("/backtest/process", {"scan_clause": clause})
        times = j["metaData"][0]["tradeTimes"]
        out = {}
        for t, lst in zip(times, j["aggregatedStockList"]):
            if lst and isinstance(lst[0], list):
                syms = [x[0] for x in lst]
            else:
                syms = lst[0::3]  # flat [symbol, cap, sector, symbol, cap, sector, ...]
            out[ist_date(t)] = syms
        return out

    # --- today's scan result with names / close -----------------------------
    def screener(self, clause):
        j = self._post("/screener/process", {"scan_clause": clause})
        return j.get("data", [])

    # --- daily OHLC for one symbol (last bar is live during market hours) ---
    def ohlc(self, symbol, size=260):
        q = f"select open, high, low, close, volume where symbol='{symbol}'"
        j = self._post("/oapi", {
            "query": q, "use_live": "1", "limit": "1", "size": str(size),
            "widget_id": "-1", "end_time": "-1", "timeframe": "Daily",
            "symbol": symbol, "scan_link": "null",
        })
        res = {}
        for part in j["groupData"][0]["results"]:
            res.update(part)
        d = [ist_date(t) for t in j["metaData"][0]["tradeTimes"]]
        bars = {"d": d, "o": res["open"], "h": res["high"], "l": res["low"], "c": res["close"]}
        # drop empty bars if any
        keep = [i for i, c in enumerate(bars["c"]) if c]
        return {k: [v[i] for i in keep] for k, v in bars.items()}

    def market_health(self):
        """Returns {date: (above, universe, pct)} for ~160 days."""
        a = self.backtest(HEALTH_CLAUSE)
        u = self.backtest(UNIVERSE_CLAUSE)
        out = {}
        for d, syms in a.items():
            if d in u and u[d]:
                out[d] = (len(syms), len(u[d]), round(100 * len(syms) / len(u[d]), 1))
        return out
