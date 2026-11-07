"""
Builds data.json for the STR100 paper-trading dashboard.

It replays the bot's rules on every STR100 signal Chartink has (about the last 8 months):
  - buy next day at open, only when Market Health >= 50%
  - max 10 stocks, capital / 10 each, calmest candle first
  - stop 10%, trail on 20 day average after day 10, exit after 40 days
Nothing here is your real money. It is a simulation ("paper trades").
"""
import json
import math
import os
import sys
from datetime import datetime

import strategy as S
from chartink import Chartink, STR_CLAUSE, IST

CAPITAL = float(os.environ.get("CAPITAL", "500000") or 500000)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")


def r2(x):
    return None if x is None else round(float(x), 2)


def simulate(health, signals, bars_by_sym, nifty, capital=CAPITAL):
    """health {d: pct}, signals {d: [sym]}, bars_by_sym {sym: raw bars}, nifty raw bars.
    Returns (trades, equity, stats)."""
    adj = {s: S.adjust_splits(b) for s, b in bars_by_sym.items()}
    sig_days = sorted(signals)
    if not sig_days:
        return [], [], {}
    days = [d for d in nifty["d"] if d >= sig_days[0]]
    ncl = dict(zip(nifty["d"], nifty["c"]))

    slot_size = capital / S.MAX_POSITIONS
    cash = capital
    open_pos = {}      # sym -> trade dict (with qty, entry_adj, exit_idx etc.)
    trades = []        # every signal, with status
    equity = []
    held_until = {}    # sym -> exit date (for "already holding" status)

    for k, day in enumerate(days):
        # 1) new buys at today's open, from yesterday's signals
        if k > 0:
            prev = days[k - 1]
            todays = []
            for sym in signals.get(prev, []):
                t = dict(symbol=sym, signal=prev, health=r2(health.get(prev)))
                a = adj.get(sym)
                if a is None or prev not in a["d"]:
                    t["status"] = "no_data"
                    trades.append(t)
                    continue
                i = a["d"].index(prev)
                t["range"] = S.candle_range(a, i)
                todays.append((t, a, i))
            todays.sort(key=lambda x: x[0]["range"])
            for t, a, i in todays:
                sym = t["symbol"]
                ei = i + 1
                # what would have happened (also for skipped signals)
                if ei < len(a["c"]):
                    entry_adj = a["o"][ei]
                    res = S.walk(a, ei, entry_adj, entry_adj * (1 - S.STOP_PCT / 100))
                    f_e = a["factor"][ei]
                    t.update(entry_date=a["d"][ei], entry=r2(entry_adj / f_e),
                             exit_date=a["d"][res["idx"]] if res["status"] == "exit" else None,
                             reason=res["reason"] or "open",
                             ret=r2(100 * (res["price"] / entry_adj - 1)),
                             days=(res["idx"] - ei), stop=r2(entry_adj / f_e * (1 - S.STOP_PCT / 100)))
                    t["exit"] = r2(t["entry"] * (1 + t["ret"] / 100))
                    if res["status"] != "exit":
                        t["exit"], t["now"] = None, r2(t["entry"] * (1 + t["ret"] / 100))
                    t["_exit_idx"], t["_entry_adj"], t["_ei"] = res["idx"], entry_adj, ei
                if sym in open_pos:
                    t["status"] = "holding"
                elif (t.get("health") or 0) < S.HEALTH_MIN:
                    t["status"] = "weak_market"
                elif "entry" not in t:
                    t["status"] = "pending"
                elif len(open_pos) >= S.MAX_POSITIONS:
                    t["status"] = "no_slot"
                else:
                    qty = int(min(slot_size, cash) // t["entry"]) if t["entry"] else 0
                    if qty <= 0:
                        t["status"] = "no_cash"
                    else:
                        t["status"] = "taken"
                        t["qty"] = qty
                        cash -= qty * t["entry"]
                        open_pos[sym] = t
                trades.append(t)

        # 2) exits during today
        for sym, t in list(open_pos.items()):
            a = adj[sym]
            if t["exit_date"] == day:
                cash += t["qty"] * t["exit"]
                t["pnl"] = r2(t["qty"] * (t["exit"] - t["entry"]))
                del open_pos[sym]

        # 3) mark to market at close
        val = cash
        for sym, t in open_pos.items():
            a = adj[sym]
            if day in a["d"]:
                j = a["d"].index(day)
                px = t["entry"] * a["c"][j] / t["_entry_adj"]
                t["_last"] = px
            val += t["qty"] * t.get("_last", t["entry"])
        equity.append([day, r2(val), r2(capital * ncl[day] / ncl[days[0]])])

    # open trades: current profit
    for t in open_pos.values():
        last = t.get("_last", t["entry"])
        t["now"] = r2(last)
        t["ret"] = r2(100 * (last / t["entry"] - 1))
        t["pnl"] = r2(t["qty"] * (last - t["entry"]))
        t["exit"] = None

    for t in trades:
        for k in [k for k in t if k.startswith("_")] + ["range"]:
            t.pop(k, None)

    taken = [t for t in trades if t["status"] == "taken"]
    closed = [t for t in taken if t.get("exit_date")]
    wins = [t for t in closed if t["ret"] > 0]
    losses = [t for t in closed if t["ret"] <= 0]
    peak, dd = 0, 0
    for _, v, _n in equity:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    final = equity[-1][1] if equity else capital
    stats = dict(
        capital=capital, value=r2(final), ret=r2(100 * (final / capital - 1)),
        nifty_ret=r2(100 * (equity[-1][2] / capital - 1)) if equity else 0,
        closed=len(closed), open=len(open_pos), wins=len(wins),
        win_rate=r2(100 * len(wins) / len(closed)) if closed else None,
        avg_win=r2(sum(t["ret"] for t in wins) / len(wins)) if wins else None,
        avg_loss=r2(sum(t["ret"] for t in losses) / len(losses)) if losses else None,
        best=max(closed, key=lambda t: t["ret"])["symbol"] if closed else None,
        best_ret=max(t["ret"] for t in closed) if closed else None,
        max_dd=r2(100 * dd), signals=len(trades),
        skipped_weak=sum(t["status"] == "weak_market" for t in trades),
        skipped_slot=sum(t["status"] == "no_slot" for t in trades),
        realized=r2(sum(t.get("pnl") or 0 for t in closed)),
        unrealized=r2(sum(t.get("pnl") or 0 for t in open_pos.values())),
    )
    return trades, equity, stats


def pack_bars(raw, start_date):
    keep = [i for i, d in enumerate(raw["d"]) if d >= start_date]
    adj = S.adjust_splits(raw)
    # send split-adjusted prices scaled to TODAY's terms so charts are continuous
    f_now = adj["factor"][-1]
    return {
        "d": [raw["d"][i] for i in keep],
        "o": [r2(adj["o"][i] / f_now) for i in keep],
        "h": [r2(adj["h"][i] / f_now) for i in keep],
        "l": [r2(adj["l"][i] / f_now) for i in keep],
        "c": [r2(adj["c"][i] / f_now) for i in keep],
        "e": [r2(adj["ema"][i] / f_now) for i in keep],
    }


def build(ck):
    h = ck.market_health()
    health = {d: v[2] for d, v in h.items()}
    signals = ck.backtest(STR_CLAUSE)
    nifty = ck.ohlc("NIFTY")
    syms = sorted({s for v in signals.values() for s in v})
    bars = {}
    for s in syms:
        try:
            bars[s] = ck.ohlc(s)
        except Exception as e:  # noqa
            print("skip", s, e, file=sys.stderr)
    trades, equity, stats = simulate(health, signals, bars, nifty)

    first_sig = {}
    for t in trades:
        first_sig.setdefault(t["symbol"], t["signal"])
    chart_bars = {}
    for s, b in bars.items():
        if s in first_sig:
            i0 = max(0, b["d"].index(first_sig[s]) - 30) if first_sig[s] in b["d"] else 0
            chart_bars[s] = pack_bars(b, b["d"][i0])

    last = max(h)
    data = dict(
        updated=datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        rules=dict(health_min=S.HEALTH_MIN, stop=S.STOP_PCT, trail_after=S.TRAIL_AFTER,
                   max_hold=S.MAX_HOLD, slots=S.MAX_POSITIONS),
        today=dict(date=last, above=h[last][0], universe=h[last][1], pct=h[last][2],
                   signals=signals.get(last, [])),
        stats=stats,
        health=[[d, health[d]] for d in sorted(health)],
        equity=equity,
        trades=trades,
        bars=chart_bars,
    )
    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"wrote {OUT}: {len(trades)} signals, {stats.get('closed')} closed trades")
    return data


if __name__ == "__main__":
    build(Chartink())
