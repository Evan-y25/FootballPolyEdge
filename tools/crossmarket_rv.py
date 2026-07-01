"""
crossmarket_rv.py — Phase 3-C: cross-market relative value / no-arbitrage
consistency. Needs NO resolution — it compares SIMULTANEOUS quotes across
related markets and measures whether the discrepancy beats the round-trip
spread and whether it converges.

Two hard (correlation-free) checks on 1x2/score/totals games:

  1. O/U ladder monotonicity (within totals):
        P(Over 0.5) >= P(Over 1.5) >= ...   (higher line must be cheaper)
     Tradable violation: ask(Over k_lo) < bid(Over k_hi)  -> buy the cheaper,
     strictly-more-likely line and sell the richer, less-likely one.

  2. Totals <-> exact-score replication (low lines are fully enumerated by
     exact-score cells, so no "Other" ambiguity):
        Over 0.5  = 1 - P(0-0)
        Under 1.5 = P(0-0)+P(1-0)+P(0-1)
        Under 2.5 = Under1.5 + P(1-1)+P(2-0)+P(0-2)
     gap = market_over_mid - score_implied_over.  Cost to arb = round-trip
     half-spread of the O/U line + the exact-score basket. Tradable if
     |gap| > cost. Convergence = does |gap| shrink over the next 5/15 min?

Usage:  venv/bin/python tools/crossmarket_rv.py [data/market.db]
"""
import sys
import numpy as np
import pandas as pd

DB = sys.argv[1] if len(sys.argv) > 1 else "data/market.db"
GRID_SEC = 30
TOL = 2 * GRID_SEC
HORIZONS = {"5m": 300, "15m": 900}

# Under(k) replicated by these exact-score cells (home-away totals fully enumerated)
UNDER_BASKET = {
    0.5: [(0, 0)],
    1.5: [(0, 0), (1, 0), (0, 1)],
    2.5: [(0, 0), (1, 0), (0, 1), (1, 1), (2, 0), (0, 2)],
}


def ff_grid(g, grid):
    gts = g.ts.values
    idx = np.searchsorted(gts, grid, side="right") - 1
    out = {}
    for c in ("bid", "ask", "bsz", "asz"):
        a = np.full(len(grid), np.nan)
        v = idx >= 0
        a[v] = g[c].values[idx[v]]
        out[c] = a
    return out


def parse_score(label):
    # "1 - 0" -> (1,0); "Other"/others -> None
    try:
        h, a = label.split(" - ")
        return int(h), int(a)
    except (ValueError, AttributeError):
        return None


def parse_ou(label):
    # "O/U 2.5" -> 2.5
    try:
        return float(label.split("O/U")[1])
    except (ValueError, IndexError):
        return None


def main():
    import sqlite3
    conn = sqlite3.connect(DB)
    slugs = [r[0] for r in conn.execute(
        "SELECT slug FROM ticks WHERE market='totals' GROUP BY slug HAVING COUNT(*)>=500")]
    print(f"games with totals data: {len(slugs)}")

    mono = {"obs": 0, "viol": 0, "viol_size": []}          # tradable ask<bid across ladder
    # box arb: buy Over(k) YES + basket cells YES -> guaranteed $1; profit if sum(asks)<1
    box = {k: {"obs": 0, "arb": 0, "profit": [], "cap": []} for k in UNDER_BASKET}

    for slug in slugs:
        r = conn.execute(
            "SELECT ts,market,label,bid,ask,bid_size,ask_size FROM ticks WHERE slug=? AND side='yes' "
            "AND market IN ('score','totals') ORDER BY ts", (slug,)).fetchall()
        if not r:
            continue
        df = pd.DataFrame(r, columns=["ts", "market", "label", "bid", "ask", "bsz", "asz"])
        df = df[(df.ask > df.bid) & (df.bid > 0) & (df.ask < 1) & (df.bsz > 0) & (df.asz > 0)]
        if df.empty:
            continue
        tmin, tmax = int(df.ts.min()), int(df.ts.max())
        grid = np.arange(tmin, tmax + 1, GRID_SEC)
        ng = len(grid)

        score = {}   # (h,a) -> {bid,ask}
        ou = {}       # k -> {bid,ask}
        for (market, label), g in df.groupby(["market", "label"]):
            g = g.sort_values("ts")
            if market == "score":
                key = parse_score(label)
                if key is not None:
                    score[key] = ff_grid(g, grid)
            else:
                k = parse_ou(label)
                if k is not None:
                    ou[k] = ff_grid(g, grid)

        def mid(d):
            return (d["bid"] + d["ask"]) / 2.0

        def half(d):
            return (d["ask"] - d["bid"]) / 2.0

        # --- 1. O/U ladder monotonicity ---
        ks = sorted(ou.keys())
        for a_i in range(len(ks)):
            for b_i in range(a_i + 1, len(ks)):
                lo, hi = ks[a_i], ks[b_i]         # lo < hi -> P(Over lo) >= P(Over hi)
                al, bl = ou[lo], ou[hi]
                m = ~np.isnan(al["ask"]) & ~np.isnan(bl["bid"])
                mono["obs"] += int(m.sum())
                # tradable violation: buy Over lo @ ask cheaper than sell Over hi @ bid
                v = m & (al["ask"] < bl["bid"])
                mono["viol"] += int(v.sum())
                if v.any():
                    mono["viol_size"].extend((bl["bid"] - al["ask"])[v].tolist())

        # --- 2. totals <-> score box arb (executable, with depth) ---
        # buy Over(k) YES + all basket cells YES @ ask -> pays $1 at settlement;
        # risk-free profit if sum(asks) < 1. capacity = min ask_size across legs.
        for kline, cells in UNDER_BASKET.items():
            if kline not in ou or any(c not in score for c in cells):
                continue
            legs = [ou[kline]] + [score[c] for c in cells]
            ask_sum = np.zeros(ng)
            cap = np.full(ng, np.inf)
            ok = np.ones(ng, dtype=bool)
            for leg in legs:
                ask_sum = ask_sum + leg["ask"]
                cap = np.minimum(cap, leg["asz"])
                ok = ok & ~np.isnan(leg["ask"]) & ~np.isnan(leg["asz"])
            box[kline]["obs"] += int(ok.sum())
            arb = ok & (ask_sum < 1.0)
            box[kline]["arb"] += int(arb.sum())
            if arb.any():
                box[kline]["profit"].extend((1.0 - ask_sum)[arb].tolist())
                box[kline]["cap"].extend(cap[arb].tolist())

    # ---------- report ----------
    print("\n" + "=" * 74)
    print("1) O/U LADDER MONOTONICITY (tradable: ask(Over lo) < bid(Over hi))")
    print("=" * 74)
    vs = np.array(mono["viol_size"]) if mono["viol_size"] else np.array([])
    print(f"pair-observations: {mono['obs']:,} | tradable violations: {mono['viol']:,} "
          f"({100*mono['viol']/max(1,mono['obs']):.3f}%)")
    if len(vs):
        print(f"violation edge size: mean {vs.mean():.4f} | median {np.median(vs):.4f} | max {vs.max():.4f}")

    print("\n" + "=" * 74)
    print("2) TOTALS <-> EXACT-SCORE BOX ARB (executable, depth-filtered)")
    print("   buy Over(k) YES + basket cells YES @ ask -> $1 at settlement.")
    print("   risk-free if sum(asks) < 1;  capacity = min ask_size across legs.")
    print("=" * 74)
    for k in UNDER_BASKET:
        d = box[k]
        if not d["obs"]:
            print(f"  Over {k}: no aligned depth data")
            continue
        n, a = d["obs"], d["arb"]
        print(f"\n  Over {k}  ({1+len(UNDER_BASKET[k])} legs, obs={n:,})")
        print(f"    risk-free box (sum asks<1): {a:,}/{n:,} = {100*a/n:.3f}%")
        if a:
            pf = np.array(d["profit"]); cp = np.array(d["cap"])
            print(f"    profit/set: mean {pf.mean():.4f} (${pf.mean():.3f}) | median {np.median(pf):.4f} | max {pf.max():.4f}")
            print(f"    capacity (min ask_size, shares): median {np.median(cp):.0f} | "
                  f"p90 {np.percentile(cp,90):.0f} | max {cp.max():.0f}")
            print(f"    est $ profit/opportunity (profit x capacity): median "
                  f"${np.median(pf*cp):.2f} | p90 ${np.percentile(pf*cp,90):.2f}")


if __name__ == "__main__":
    main()
