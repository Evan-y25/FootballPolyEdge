"""
factor_lab.py — Phase 1 (feature panel + data-quality report) + Phase 2 (factor IC).

Target B: forward change in a line's YES mid (implied prob) over 1/5/15 min.
This needs NO resolution — it's future price vs current price — so every game
with enough tick history is usable (incl. in-progress multi-market games).

Per game we build a [line x 30s-grid] panel, compute microstructure + a couple
cross-market factors and forward targets, then report:
  Phase 1 — data quality (rows, per-market coverage, crossed/invalid drop rate,
            forward-label coverage, fraction of moves that beat the spread).
  Phase 2 — factor IC: Spearman corr(factor, forward return) computed PER GAME
            then aggregated (mean IC, IC_IR = mean/std, n_games), split by
            pre-match / live regime. Per-game grouping avoids intra-game leakage.

Usage:  venv/bin/python tools/factor_lab.py [data/market.db]
"""
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

DB = sys.argv[1] if len(sys.argv) > 1 else "data/market.db"

GRID_SEC = 30
HORIZONS = {"1m": 60, "5m": 300, "15m": 900}
TOL = 2 * GRID_SEC          # forward/past lookup tolerance
WARMUP_SEC = 900            # drop first 15 min of each line (momentum warmup)
MIN_TICKS = 2000            # skip tiny games
MIN_LINE_ROWS = 40          # skip a line with too few grid rows
MIN_PAIRS = 200             # min (factor,target) pairs to compute a per-game IC

FACTORS = ["obi", "rel_spread", "depth_log", "extremeness",
           "mom_1m", "mom_5m", "mom_15m", "vol_5m", "grp_overround", "ttk_min"]


def kickoff_ts(kickoff):
    if not kickoff:
        return None
    try:
        return int(datetime.fromisoformat(kickoff.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def build_game_panel(conn, slug, ko):
    import sqlite3  # noqa
    rows = conn.execute(
        "SELECT ts, market, label, bid, ask, bid_size, ask_size FROM ticks "
        "WHERE slug=? AND side='yes' ORDER BY ts", (slug,)).fetchall()
    if len(rows) < MIN_TICKS:
        return None, (0, 0, 0)
    raw = pd.DataFrame(rows, columns=["ts", "market", "label", "bid", "ask", "bsz", "asz"])
    n_raw = len(raw)
    # clean: valid book, not crossed, mid in (0,1), real depth
    raw = raw[(raw.ask > 0) & (raw.bid >= 0)]
    crossed = int((raw.bid >= raw.ask).sum())
    raw = raw[(raw.ask > raw.bid) & (raw.bid > 0) & (raw.bsz > 0) & (raw.asz > 0)]
    if raw.empty:
        return None, (n_raw, crossed, 0)
    tmin, tmax = int(raw.ts.min()), int(raw.ts.max())
    if tmax - tmin < 2 * max(HORIZONS.values()):
        return None, (n_raw, crossed, 0)
    grid = np.arange(tmin, tmax + 1, GRID_SEC)

    frames = []
    for (market, label), g in raw.groupby(["market", "label"], sort=False):
        g = g.sort_values("ts")
        gts = g.ts.values
        # forward-fill each column onto the grid
        idx = np.searchsorted(gts, grid, side="right") - 1
        valid = idx >= 0
        if valid.sum() < MIN_LINE_ROWS:
            continue
        gi = idx[valid]
        ts = grid[valid].astype(float)
        bid = g.bid.values[gi]; ask = g.ask.values[gi]
        bsz = g.bsz.values[gi]; asz = g.asz.values[gi]
        mid = (bid + ask) / 2.0
        keep = (mid > 0) & (mid < 1) & (ask > bid) & (bsz > 0) & (asz > 0)
        ts, bid, ask, bsz, asz, mid = ts[keep], bid[keep], ask[keep], bsz[keep], asz[keep], mid[keep]
        n = len(ts)
        if n < MIN_LINE_ROWS:
            continue

        def at(off):
            tgt = ts + off
            j = np.clip(np.searchsorted(ts, tgt, side="left"), 0, n - 1)
            ok = np.abs(ts[j] - tgt) <= TOL
            return np.where(ok, mid[j], np.nan)

        d = pd.DataFrame({
            "ts": ts, "market": market, "label": label, "mid": mid,
            "obi": (bsz - asz) / (bsz + asz),
            "rel_spread": (ask - bid) / mid,
            "depth_log": np.log1p(bsz + asz),
            "extremeness": np.abs(mid - 0.5),
            "mom_1m": mid - at(-60), "mom_5m": mid - at(-300), "mom_15m": mid - at(-900),
            "half_spread": (ask - bid) / 2.0,
            "_ask": ask,
        })
        d["vol_5m"] = pd.Series(mid).rolling(10, min_periods=5).std().values
        for name, h in HORIZONS.items():
            d["fwd_" + name] = at(h) - mid
        frames.append(d)

    if not frames:
        return None, (n_raw, crossed, 0)
    panel = pd.concat(frames, ignore_index=True)
    # cross-market factor: sum of asks across the lines of a market group at each ts
    panel["grp_overround"] = panel.groupby(["market", "ts"])._ask.transform("sum")
    panel.drop(columns="_ask", inplace=True)
    # regime + warmup
    panel["ttk_min"] = (ko - panel.ts) / 60.0 if ko else np.nan
    panel["is_live"] = (panel.ts >= ko) if ko else False
    panel = panel[panel.ts >= tmin + WARMUP_SEC]
    return panel, (n_raw, crossed, len(panel))


def spearman(x, y):
    m = x.notna() & y.notna()
    if int(m.sum()) < MIN_PAIRS:
        return np.nan, int(m.sum())
    xr, yr = x[m].rank(), y[m].rank()
    if xr.std() == 0 or yr.std() == 0:
        return np.nan, int(m.sum())
    return float(np.corrcoef(xr, yr)[0, 1]), int(m.sum())


def main():
    import sqlite3
    conn = sqlite3.connect(DB)
    games = conn.execute(
        "SELECT t.slug, COUNT(*) c, g.kickoff FROM ticks t LEFT JOIN games g ON g.slug=t.slug "
        "GROUP BY t.slug HAVING c>=? ORDER BY c DESC", (MIN_TICKS,)).fetchall()

    ic = {}                       # (factor, horizon, regime) -> [per-game ic]
    icm = {}                      # (market, factor, horizon) -> [per-game ic]
    icm_meta = {}                 # market -> [games, rows]
    dq = {"games": 0, "raw": 0, "crossed": 0, "rows": 0,
          "per_market": {}, "beat_cost_5m": [0, 0], "half_spreads": []}

    for slug, c, kickoff in games:
        ko = kickoff_ts(kickoff)
        panel, (n_raw, crossed, n_rows) = build_game_panel(conn, slug, ko)
        dq["raw"] += n_raw; dq["crossed"] += crossed
        if panel is None or panel.empty:
            continue
        dq["games"] += 1; dq["rows"] += len(panel)
        for m, k in panel.market.value_counts().items():
            dq["per_market"][m] = dq["per_market"].get(m, 0) + int(k)
        # cost context on 5m horizon
        c5 = panel[["fwd_5m", "half_spread"]].dropna()
        dq["beat_cost_5m"][0] += int((c5.fwd_5m.abs() > c5.half_spread).sum())
        dq["beat_cost_5m"][1] += len(c5)
        dq["half_spreads"].append(float(panel.half_spread.median()))

        regimes = {"all": panel}
        if ko:
            regimes["pre"] = panel[~panel.is_live]
            regimes["live"] = panel[panel.is_live]
        for f in FACTORS:
            for hz in HORIZONS:
                tgt = "fwd_" + hz
                for rg, sub in regimes.items():
                    if f not in sub or sub.empty:
                        continue
                    val, npair = spearman(sub[f], sub[tgt])
                    if not np.isnan(val):
                        ic.setdefault((f, hz, rg), []).append(val)

        # per-market-group IC (is the signal on liquid tradable books, or just score?)
        for mk, msub in panel.groupby("market"):
            if len(msub) < MIN_PAIRS:
                continue
            meta = icm_meta.setdefault(mk, [0, 0])
            meta[0] += 1; meta[1] += len(msub)
            for f in FACTORS:
                for hz in HORIZONS:
                    val, _ = spearman(msub[f], msub["fwd_" + hz])
                    if not np.isnan(val):
                        icm.setdefault((mk, f, hz), []).append(val)

    # ---------- Phase 1: data quality ----------
    print("=" * 78)
    print("PHASE 1 — DATA QUALITY")
    print("=" * 78)
    print(f"games used (>= {MIN_TICKS} ticks): {dq['games']}")
    print(f"raw yes-ticks scanned: {dq['raw']:,} | crossed(bid>=ask) dropped: "
          f"{dq['crossed']:,} ({100*dq['crossed']/max(1,dq['raw']):.2f}%)")
    print(f"panel rows (line x 30s grid, post-clean/warmup): {dq['rows']:,}")
    if dq["half_spreads"]:
        print(f"median half-spread across games: {np.median(dq['half_spreads']):.4f}  "
              f"(≈ per-share cost to cross)")
    bc, bt = dq["beat_cost_5m"]
    print(f"5m moves that BEAT the spread (|Δmid| > half-spread): "
          f"{bc:,}/{bt:,} = {100*bc/max(1,bt):.1f}%")
    print("\nrows per market group:")
    for m, k in sorted(dq["per_market"].items(), key=lambda x: -x[1]):
        print(f"  {m:16} {k:>10,}")

    # ---------- Phase 2: factor IC ----------
    print("\n" + "=" * 78)
    print("PHASE 2 — FACTOR IC  (Spearman per-game, then averaged)")
    print("  meanIC = avg per-game rank corr(factor, forward Δmid)")
    print("  IC_IR  = meanIC / std(IC)  (stability; |IR|>~0.3 interesting)")
    print("=" * 78)
    for rg in ["all", "pre", "live"]:
        rows = []
        for (f, hz, r), vals in ic.items():
            if r != rg or len(vals) < 3:
                continue
            a = np.array(vals)
            rows.append((f, hz, a.mean(), a.std(ddof=0), a.mean() / (a.std(ddof=0) or 1e-9), len(a)))
        if not rows:
            continue
        # sort by |meanIC| at 5m, else overall |meanIC|
        rows.sort(key=lambda x: -abs(x[2]))
        print(f"\n--- regime: {rg} ---")
        print(f"{'factor':14}{'horizon':8}{'meanIC':>9}{'IC_IR':>8}{'n_games':>9}")
        for f, hz, mic, sd, ir, n in rows:
            flag = "  <<" if abs(mic) >= 0.03 and abs(ir) >= 0.3 else ""
            print(f"{f:14}{hz:8}{mic:>9.4f}{ir:>8.2f}{n:>9}{flag}")

    # ---------- Phase 2b: IC by market group ----------
    print("\n" + "=" * 78)
    print("PHASE 2b — FACTOR IC BY MARKET GROUP  (all regimes pooled)")
    print("  Does the signal hold on the liquid/tradable books (1x2, totals),")
    print("  or is the overall IC just driven by the dominant score lines?")
    print("=" * 78)

    def mean_ic(mk, f, hz):
        v = icm.get((mk, f, hz), [])
        if len(v) < 3:
            return None, None, 0
        a = np.array(v)
        return a.mean(), a.mean() / (a.std(ddof=0) or 1e-9), len(a)

    # markets ordered by tradability/interest, then by data volume
    order = ["1x2", "totals", "team_totals", "spread", "score", "first_to_score",
             "btts", "halves", "penalty", "extra_time", "team_to_advance"]
    present = [m for m in order if m in icm_meta] + \
              [m for m in icm_meta if m not in order]
    for mk in present:
        g, rows_n = icm_meta[mk]
        if g < 3:
            continue
        print(f"\n--- market: {mk}  (n_games={g}, rows={rows_n:,}) ---")
        print(f"{'factor':14}{'IC@1m':>9}{'IC@5m':>9}{'IC@15m':>9}{'IC_IR@5m':>10}{'n':>5}")
        table = []
        for f in FACTORS:
            m1, _, _ = mean_ic(mk, f, "1m")
            m5, ir5, n5 = mean_ic(mk, f, "5m")
            m15, _, _ = mean_ic(mk, f, "15m")
            key = abs(m5) if m5 is not None else 0
            table.append((key, f, m1, m5, m15, ir5, n5))
        table.sort(key=lambda x: -x[0])
        for _, f, m1, m5, m15, ir5, n5 in table:
            fmt = lambda v: f"{v:>9.4f}" if v is not None else f"{'—':>9}"
            flag = "  <<" if (m5 is not None and abs(m5) >= 0.03
                              and ir5 is not None and abs(ir5) >= 0.3) else ""
            print(f"{f:14}{fmt(m1)}{fmt(m5)}{fmt(m15)}"
                  f"{(f'{ir5:>10.2f}' if ir5 is not None else f'{chr(8212):>10}')}{n5:>5}{flag}")


if __name__ == "__main__":
    main()
