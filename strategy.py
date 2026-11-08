"""
Pure trading rules. No internet, no Telegram. Easy to test.

Rules (from the backtest, Mar to Nov 2026, updated with the 10-year NSE test, Nov 2026):
  BUY   : STR100 signal on a day when Market Health >= 50% AND health is not below its own
          average of the last 10 days (market getting better, not worse).
          Buy the SAME DAY before the close (3:07 pm alert). Signals that only show up at the
          close are bought next morning at the open.
  SKIP  : stocks that move too wildly (average daily range > 8.5% over the 20 days before the
          signal) or are too stretched (close > 50% above the 20-day EMA), and stocks locked
          at the upper circuit (nobody is selling, you can't buy).
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
MAX_ATR_PCT = 8.5    # skip a signal if its average daily range (20 days before) is above this
MAX_EXT_PCT = 50.0   # skip a signal if the close is this much above its 20-day EMA
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
    open is treated as a corporate action. A real crash that big looks the same, so the bot
    warns you when this happens to a stock you hold (see bot.split_warning).
    Also returns the factor per bar (adjusted / raw) so user prices can be converted."""
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


def walk(adj, ei, entry, stop, base=None, gap_first=False):
    """Walk forward from bar index `ei` using adjusted prices.
    base: bar the day count starts from (the buy bar). gap_first: also check a gap below the
    stop on bar ei (used when you bought at the previous day's close).
    Returns dict with exit info, or status 'open' if nothing triggered yet."""
    base = ei if base is None else base
    n = len(adj["c"]) - 1
    for k in range(ei, n + 1):
        if (k > ei or gap_first) and adj["o"][k] <= stop:
            return dict(status="exit", reason="stop_gap", idx=k, price=adj["o"][k])
        if adj["l"][k] <= stop:
            return dict(status="exit", reason="stop", idx=k, price=stop)
        if k - base >= MAX_HOLD:
            return dict(status="exit", reason="time", idx=k, price=adj["c"][k])
        if k - base >= TRAIL_AFTER and adj["c"][k] < adj["ema"][k]:
            return dict(status="exit", reason="trail", idx=k, price=adj["c"][k])
    return dict(status="open", reason=None, idx=n, price=adj["c"][n])


def atr_pct(bars, i, n=20):
    """Average true range (% of price) of the n days BEFORE bar i. None if not enough data."""
    if i - n < 1:
        return None
    t = []
    for k in range(i - n, i):
        pc = bars["c"][k - 1]
        tr = max(bars["h"][k] - bars["l"][k], abs(bars["h"][k] - pc), abs(bars["l"][k] - pc))
        t.append(tr / bars["c"][k] if bars["c"][k] else 0)
    return 100 * sum(t) / len(t)


def skip_reason(adj, i):
    """Why a signal on bar i should be skipped (None = take it). Uses adjusted bars with 'ema'."""
    a = atr_pct(adj, i)
    if a is not None and a > MAX_ATR_PCT:
        return f"too jumpy (moves {a:.1f}% a day on average)"
    e = adj["ema"][i]
    if e and 100 * (adj["c"][i] / e - 1) > MAX_EXT_PCT:
        return f"too stretched ({100 * (adj['c'][i] / e - 1):.0f}% above its 20-day average)"
    return None


def upper_locked(bars, i):
    """Bar i traded at one price all day, up from yesterday: locked at the upper circuit."""
    if i < 1 or not bars["c"][i - 1]:
        return False
    return bars["h"][i] == bars["l"][i] and bars["o"][i] / bars["c"][i - 1] - 1 > 0.015


def evaluate_position(adj, entry_date, entry_price_raw, at_close=False):
    """Live check of a position the user holds.
    entry_price_raw is what the user actually paid (not split adjusted).
    at_close: bought near the close of entry_date, so that day's low doesn't count."""
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
    n = len(adj["c"]) - 1
    if at_close:
        res = (walk(adj, ei + 1, entry, stop, base=ei, gap_first=True) if ei < n
               else dict(status="open", reason=None, idx=n, price=adj["c"][n]))
    else:
        res = walk(adj, ei, entry, stop)
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


def backtest(signals, health, bars_by_symbol, health_min=HEALTH_MIN, rising=None, at_close=True, filters=True):
    """Signal-by-signal backtest (no slot limit), one open trade per stock.
    signals: list of (date, symbol) in date order. Buys at the signal day's close (at_close)
    or the next open. Returns list of trades."""
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
        if filters and skip_reason(adj, i):
            continue
        if at_close:
            if upper_locked(adj, i) or i + 1 >= len(adj["c"]):
                continue
            ei, entry = i, adj["c"][i]
            res = walk(adj, i + 1, entry, entry * (1 - STOP_PCT / 100), base=i, gap_first=True)
        else:
            ei = i + 1
            if ei >= len(adj["c"]) or upper_locked(adj, ei):
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
