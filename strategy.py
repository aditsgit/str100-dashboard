"""
Pure trading rules. No internet, no Telegram. Easy to test.

Rules (from the backtest, Mar to Nov 2026, rising rule from the 10-year test, Nov 2026):
  BUY   : STR100 signal on a day when Market Health >= 50% AND health is not below its own
          average of the last 10 days (market getting better, not worse). Buy next morning at open.
  RANK  : When there are more signals than free slots, buy the "calmest" candle first
          (smallest (high - low) / close on the signal day).
  SIZE  : 10% of capital per stock, max 10 stocks.
  STOP  : 10% below your buy price.
  TRAIL : After 10 trading days, sell when the stock CLOSES below its 20-day EMA.
  TIME  : Sell after 40 trading days whatever happens.
  NOTE  : Market Health only decides NEW buys. It never forces a sell.
"""

HEALTH_MIN = 50.0
RISING_AVG = 10      # health must also be >= its average of the previous 10 days (0 = off)
STOP_PCT = 10.0
TRAIL_AFTER = 10
MAX_HOLD = 40
EMA_LEN = 20
MAX_POSITIONS = 10


def ema(values, n):
    k = 2 / (n + 1)
    out = []
    for i, v in enumerate(values):
        out.append(v if i == 0 else v * k + out[-1] * (1 - k))
    return out


def adjust_splits(bars):
    """Return a copy of bars with prices before a split / bonus scaled down.
    A gap of more than 40% down (or 80% up) between yesterday's close and today's
    open is treated as a corporate action. Also returns the factor per bar
    (adjusted / raw) so user prices can be converted."""
    o, h, l, c = (list(bars[k]) for k in ("o", "h", "l", "c"))
    n = len(c)
    factor = [1.0] * n
    for k in range(n - 1, 0, -1):
        if not bars["c"][k - 1]:
            continue
        g = bars["o"][k] / bars["c"][k - 1]
        if g < 0.6 or g > 1.8:
            for m in range(k):
                o[m] *= g
                h[m] *= g
                l[m] *= g
                c[m] *= g
                factor[m] *= g
    adj = {"d": list(bars["d"]), "o": o, "h": h, "l": l, "c": c, "factor": factor}
    adj["ema"] = ema(c, EMA_LEN)
    return adj


def candle_range(bars, i):
    """Smaller = calmer breakout candle = higher priority."""
    return (bars["h"][i] - bars["l"][i]) / bars["c"][i] if bars["c"][i] else 9


def walk(adj, ei, entry, stop):
    """Walk forward from entry bar index `ei` using adjusted prices.
    Returns dict with exit info, or status 'open' if nothing triggered yet."""
    n = len(adj["c"]) - 1
    for k in range(ei, n + 1):
        if k > ei and adj["o"][k] <= stop:
            return dict(status="exit", reason="stop_gap", idx=k, price=adj["o"][k])
        if adj["l"][k] <= stop:
            return dict(status="exit", reason="stop", idx=k, price=stop)
        if k - ei >= MAX_HOLD:
            return dict(status="exit", reason="time", idx=k, price=adj["c"][k])
        if k - ei >= TRAIL_AFTER and adj["c"][k] < adj["ema"][k]:
            return dict(status="exit", reason="trail", idx=k, price=adj["c"][k])
    return dict(status="open", reason=None, idx=n, price=adj["c"][n])


def evaluate_position(adj, entry_date, entry_price_raw):
    """Live check of a position the user holds.
    entry_price_raw is what the user actually paid (not split adjusted)."""
    if entry_date not in adj["d"]:
        later = [i for i, d in enumerate(adj["d"]) if d >= entry_date]
        if not later:
            return None
        ei = later[0]
    else:
        ei = adj["d"].index(entry_date)
    f = adj["factor"][ei]
    entry = entry_price_raw * f
    stop = entry * (1 - STOP_PCT / 100)
    res = walk(adj, ei, entry, stop)
    n = len(adj["c"]) - 1
    days = (res["idx"] if res["status"] == "exit" else n) - ei
    last = adj["c"][n]
    res.update(
        days_held=days,
        entry_adj=entry,
        stop_adj=stop,
        stop_raw=stop / adj["factor"][n],  # in today's price terms
        last_close=last,
        pnl_pct=100 * (last / entry - 1),
        ema=adj["ema"][n],
        trail_active=days >= TRAIL_AFTER,
        days_left=max(0, MAX_HOLD - days),
        split_after_entry=abs(adj["factor"][ei] - adj["factor"][n]) > 1e-9,
    )
    return res


def health_avg(health, d, n=None):
    """Average market health of the n days before d. health: {date: pct}."""
    n = RISING_AVG if n is None else n
    ds = sorted(x for x in health if x < d)[-n:] if n else []
    return sum(health[x] for x in ds) / len(ds) if ds else None


def can_buy(health, d, health_min=HEALTH_MIN, n=None):
    """New buys allowed on day d? Health >= health_min and not below its n-day average."""
    p = health.get(d)
    if p is None or p < health_min:
        return False
    a = health_avg(health, d, n)
    return a is None or p >= a


def backtest(signals, health, bars_by_symbol, health_min=HEALTH_MIN, rising=None):
    """Signal-by-signal backtest (no slot limit), one open trade per stock.
    signals: list of (date, symbol) in date order.
    Returns list of trades."""
    adj_cache = {}
    busy_until = {}
    trades = []
    for d, s in signals:
        if not can_buy(health, d, health_min, rising) or s not in bars_by_symbol:
            continue
        adj = adj_cache.setdefault(s, adjust_splits(bars_by_symbol[s]))
        if d not in adj["d"]:
            continue
        i = adj["d"].index(d)
        if s in busy_until and busy_until[s] >= i:
            continue
        ei = i + 1
        if ei >= len(adj["c"]):
            continue
        entry = adj["o"][ei]
        res = walk(adj, ei, entry, entry * (1 - STOP_PCT / 100))
        busy_until[s] = res["idx"]
        trades.append(dict(symbol=s, signal=d, entry_date=adj["d"][ei],
                           exit_date=adj["d"][res["idx"]], reason=res["reason"] or "open",
                           ret=100 * (res["price"] / entry - 1)))
    return trades


def summarize(trades):
    if not trades:
        return dict(n=0)
    r = [t["ret"] for t in trades]
    w = [x for x in r if x > 0]
    l = [x for x in r if x <= 0]
    return dict(
        n=len(r), win=round(100 * len(w) / len(r)), avg=round(sum(r) / len(r), 2),
        avg_win=round(sum(w) / len(w), 2) if w else 0,
        avg_loss=round(sum(l) / len(l), 2) if l else 0,
        pf=round(sum(w) / -sum(l), 2) if l and sum(l) else None,
        worst=round(min(r), 2),
    )


def position_size(capital, price, max_positions=MAX_POSITIONS):
    slot = capital / max_positions
    qty = int(slot // price) if price > 0 else 0
    return qty, slot
