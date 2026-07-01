"""
obi_maker.py — Phase 3-B: is OBI on 1X2 monetizable?

OBI has real predictive IC on 1x2 (IC_IR ~1.9), but 98% of short moves are
smaller than the half-spread, so a TAKER (cross-spread) can't profit. The only
remaining path is MAKER (post passively, earn the spread, use OBI to pick side).

We test three PnL definitions per signal (OBI in top/bottom decile), fee=0
(Polymarket maker fee is ~0; the real cost is adverse selection):

  1. taker            : cross the spread now. pnl = signal*fwd_ret - half_spread
  2. maker optimistic : fill at the touch, unconditional. pnl = half_spread + signal*fwd_ret
  3. maker adverse-fill: a passive BUY@bid only fills when the ask later drops to
                         your bid (price came DOWN to you) within a 2-min window
                         -> fills are adversely selected. Entry=bid, exit=mid at
                         fill_time+H. This is the decisive, honest test.
  baseline naive maker: same adverse-fill model but posting on EVERY row (no OBI)
                        -> shows raw spread capture net of adverse selection.

Usage:  venv/bin/python tools/obi_maker.py [data/market.db]
"""
import sys
import numpy as np
import pandas as pd

DB = sys.argv[1] if len(sys.argv) > 1 else "data/market.db"
GRID_SEC = 30
TOL = 2 * GRID_SEC
H = 300            # hold horizon for the exit mark (5 min)
FILL_WIN = 120     # passive order lives 2 min waiting for a fill
WARMUP = 900
MIN_LINE = 40


def main():
    import sqlite3
    conn = sqlite3.connect(DB)
    slugs = [r[0] for r in conn.execute(
        "SELECT t.slug FROM ticks t WHERE market='1x2' GROUP BY t.slug "
        "HAVING COUNT(*)>=500")]

    rows = []   # obi, half, fwd_h, buy_fill_pnl, sell_fill_pnl
    for slug in slugs:
        r = conn.execute(
            "SELECT ts,label,bid,ask,bid_size,ask_size FROM ticks "
            "WHERE slug=? AND market='1x2' AND side='yes' ORDER BY ts", (slug,)).fetchall()
        if len(r) < 500:
            continue
        df = pd.DataFrame(r, columns=["ts", "label", "bid", "ask", "bsz", "asz"])
        df = df[(df.ask > df.bid) & (df.bid > 0) & (df.bsz > 0) & (df.asz > 0)]
        if df.empty:
            continue
        tmin, tmax = int(df.ts.min()), int(df.ts.max())
        if tmax - tmin < 2 * H:
            continue
        grid = np.arange(tmin, tmax + 1, GRID_SEC)
        for label, g in df.groupby("label"):
            g = g.sort_values("ts")
            gts = g.ts.values
            idx = np.searchsorted(gts, grid, side="right") - 1
            v = idx >= 0
            if v.sum() < MIN_LINE:
                continue
            gi = idx[v]
            ts = grid[v].astype(float)
            bid = g.bid.values[gi]; ask = g.ask.values[gi]
            bsz = g.bsz.values[gi]; asz = g.asz.values[gi]
            mid = (bid + ask) / 2.0
            keep = (mid > 0) & (mid < 1) & (ask > bid) & (bsz > 0) & (asz > 0) & (ts >= tmin + WARMUP)
            n = len(ts)
            obi = (bsz - asz) / (bsz + asz)
            half = (ask - bid) / 2.0

            def mid_at(t):
                j = np.clip(np.searchsorted(ts, t, side="left"), 0, n - 1)
                return mid[j] if abs(ts[j] - t) <= TOL else np.nan

            for i in range(n):
                if not keep[i]:
                    continue
                fwd = mid_at(ts[i] + H)
                if np.isnan(fwd):
                    continue
                fwd_ret = fwd - mid[i]
                # adverse-fill: passive BUY@bid[i] fills when ask drops to <= bid[i]
                buy_pnl = np.nan
                sell_pnl = np.nan
                j = i + 1
                while j < n and ts[j] <= ts[i] + FILL_WIN:
                    if np.isnan(buy_pnl) and ask[j] <= bid[i]:
                        ex = mid_at(ts[j] + H)
                        if not np.isnan(ex):
                            buy_pnl = ex - bid[i]
                    if np.isnan(sell_pnl) and bid[j] >= ask[i]:
                        ex = mid_at(ts[j] + H)
                        if not np.isnan(ex):
                            sell_pnl = ask[i] - ex
                    if not (np.isnan(buy_pnl) and np.isnan(sell_pnl)):
                        pass
                    j += 1
                rows.append((obi[i], half[i], fwd_ret, buy_pnl, sell_pnl))

    A = pd.DataFrame(rows, columns=["obi", "half", "fwd", "buy_fill", "sell_fill"])
    print(f"1X2 maker study: {len(A):,} signal rows, {len(slugs)} games, H={H//60}min, "
          f"fill_win={FILL_WIN//60}min, fee=0")
    print(f"median half-spread: {A.half.median():.4f}")

    # ---- OBI decile -> forward move (magnitude) ----
    print("\n--- OBI decile -> mean forward Δmid (5m) ---")
    A["dec"] = pd.qcut(A.obi, 10, labels=False, duplicates="drop")
    tab = A.groupby("dec").agg(meanOBI=("obi", "mean"), fwd=("fwd", "mean"),
                               half=("half", "mean"), n=("obi", "size"))
    for d, r in tab.iterrows():
        print(f"  decile {int(d):2}  OBI={r.meanOBI:+.3f}  E[Δmid_5m]={r.fwd:+.5f}  "
              f"(half-spread {r.half:.4f})  n={int(r.n):,}")

    q90, q10 = A.obi.quantile(0.9), A.obi.quantile(0.1)
    buy = A[A.obi >= q90]     # strong buy pressure -> post BUY
    sell = A[A.obi <= q10]    # strong sell pressure -> post SELL

    def stat(x):
        x = pd.Series(x).dropna()
        return (x.mean() if len(x) else np.nan, (x > 0).mean() if len(x) else np.nan, len(x))

    def report(name, pnl_series, base_n=None):
        m, w, k = stat(pnl_series)
        fill = f"  fill%={100*k/base_n:.0f}" if base_n else ""
        print(f"  {name:34}{m:>+10.5f}{'' if np.isnan(w) else f'  win%={100*w:.0f}'}  n={k:,}{fill}")

    print(f"\n--- strategy PnL per trade (signal = OBI top/bottom decile; q90={q90:+.3f}, q10={q10:+.3f}) ---")
    # taker: cross spread now
    taker = pd.concat([buy.fwd - buy.half, -sell.fwd - sell.half])
    report("taker (cross spread)", taker)
    # optimistic maker: touch fill, unconditional
    opt = pd.concat([buy.half + buy.fwd, sell.half - sell.fwd])
    report("maker optimistic (touch fill)", opt)
    # adverse-fill maker: only fills that actually occurred
    adv = pd.concat([buy.buy_fill, sell.sell_fill])
    signal_n = len(buy) + len(sell)
    report("maker adverse-fill (DECISIVE)", adv, base_n=signal_n)
    # baseline: naive maker posting on every row (no OBI), adverse-fill
    naive = pd.concat([A.buy_fill, A.sell_fill])
    report("baseline naive maker (no OBI)", naive, base_n=2 * len(A))


if __name__ == "__main__":
    main()
