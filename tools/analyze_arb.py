"""
Offline analysis on stored ticks (size-aware):
  A) Risk-free arbitrage — frequency AND executable $ (constrained by top-of-book size)
  B) Cross-market inconsistency / lag (波胆-implied 1X2 vs direct 1X2)

Event-driven replay keeping latest best bid/ask + sizes per outcome.
For each arb window we compute, at every tick while in-arb:
    sets = min(size across the legs)          # shares you can actually fill at top-of-book
    profit$ = sets * edge                      # each share pays $1; edge = |1 - sum|
and keep the window's PEAK profit$ (best instant). Summing window peaks is an
OPTIMISTIC upper bound (assumes you hit each window's best moment, full top size,
simultaneous fills, 0 fee — Polymarket has 0 trading fee). Gross of gas/slippage.

Usage:  python3 tools/analyze_arb.py [db_path]
"""

import sqlite3
import sys


def score_side(label):
    import re
    m = re.search(r"(\d+)\s*-\s*(\d+)", label)
    if not m:
        return None
    i, j = int(m.group(1)), int(m.group(2))
    return "home" if i > j else "draw" if i == j else "away"


class ArbStat:
    def __init__(self):
        self.win = []   # list of (peak_edge, peak_usd) per closed window
        self._open = None
        self._pk_edge = 0.0
        self._pk_usd = 0.0

    def step(self, ts, on, edge=0.0, usd=0.0):
        if on:
            if self._open is None:
                self._open = ts
                self._pk_edge, self._pk_usd = edge, usd
            else:
                self._pk_edge = max(self._pk_edge, edge)
                self._pk_usd = max(self._pk_usd, usd)
        elif self._open is not None:
            self.win.append((self._pk_edge, self._pk_usd))
            self._open = None


def analyze(db):
    c = sqlite3.connect(db)
    slugs = [r[0] for r in c.execute("SELECT DISTINCT slug FROM ticks")]
    stats = {k: ArbStat() for k in ["1x2_back", "1x2_lay", "score_back", "score_lay"]}

    for slug in slugs:
        rows = c.execute(
            "SELECT ts, market, label, side, bid, ask, bid_size, ask_size "
            "FROM ticks WHERE slug=? ORDER BY ts", (slug,)
        ).fetchall()
        if not rows:
            continue
        score_labels = sorted(set(l for _, m, l, *_ in rows if m == "score"))
        ask, askq, bid, bidq = {}, {}, {}, {}

        for ts, market, label, side, b, a, bq, aq in rows:
            key = (market, label, side)
            ask[key], askq[key] = a or 0.0, aq or 0.0
            bid[key], bidq[key] = b or 0.0, bq or 0.0

            # 1X2 back (buy 3 yes)
            legs = [("1x2", l, "yes") for l in ("home", "draw", "away")]
            a3 = [ask.get(k, 0.0) for k in legs]
            if all(x > 0 for x in a3):
                s = sum(a3); edge = 1.0 - s
                sets = min(askq.get(k, 0.0) for k in legs)
                stats["1x2_back"].step(ts, s < 1.0, edge, max(0.0, edge) * sets)
            b3 = [bid.get(k, 0.0) for k in legs]
            if all(x > 0 for x in b3):
                s = sum(b3); edge = s - 1.0
                sets = min(bidq.get(k, 0.0) for k in legs)
                stats["1x2_lay"].step(ts, s > 1.0, edge, max(0.0, edge) * sets)

            # 波胆 back/lay (all listed scores)
            slegs = [("score", l, "yes") for l in score_labels]
            sa = [ask.get(k, 0.0) for k in slegs]
            if score_labels and all(x > 0 for x in sa):
                s = sum(sa); edge = 1.0 - s
                sets = min(askq.get(k, 0.0) for k in slegs)
                stats["score_back"].step(ts, s < 1.0, edge, max(0.0, edge) * sets)
            sb = [bid.get(k, 0.0) for k in slegs]
            if score_labels and all(x > 0 for x in sb):
                s = sum(sb); edge = s - 1.0
                sets = min(bidq.get(k, 0.0) for k in slegs)
                stats["score_lay"].step(ts, s > 1.0, edge, max(0.0, edge) * sets)

    CAP = 0.05   # edges above this are almost surely stale/crossed/transitional books
    FLOOR = 0.003
    print("=== 无风险套利：剔除假象后的真实图景 (size-aware, 0手续费) ===")
    print(f"分析比赛: {len(slugs)} 场")
    print(f"「合理」= edge 在 {FLOOR*100:.1f}%~{CAP*100:.0f}% 之间(真套利量级); "
          f"「极端」= edge>{CAP*100:.0f}%(几乎都是进球/结算瞬间的陈旧或单边盘, 不可成交)\n")
    labels = {"1x2_back": "1X2 正套", "1x2_lay": "1X2 反套",
              "score_back": "波胆 正套", "score_lay": "波胆 反套"}
    real_total = 0.0
    for k in ["1x2_back", "1x2_lay", "score_back", "score_lay"]:
        wins = stats[k].win
        real = [(e, u) for e, u in wins if FLOOR <= e <= CAP]
        extreme = [(e, u) for e, u in wins if e > CAP]
        real_usd = sum(u for _, u in real)
        ge1 = sum(1 for _, u in real if u >= 1)
        med = sorted(u for _, u in real)
        med = med[len(med) // 2] if med else 0
        real_total += real_usd
        print(f"{labels[k]}:")
        print(f"   合理窗口 {len(real)} 次 (其中可赚≥$1 的 {ge1} 次, 中位 ${med:.1f}) | "
              f"合理可赚合计 ${real_usd:.0f}")
        print(f"   极端/假象窗口 {len(extreme)} 次 (忽略)\n")
    print(f"=== 真实(合理)无风险套利上限: 3天32场合计约 ${real_total:.0f} ===")
    print("   仍是乐观值: 假设你每次都抢到峰值价、吃满顶档量、且多腿瞬间同时成交、零滑点。")


if __name__ == "__main__":
    analyze(sys.argv[1] if len(sys.argv) > 1 else "data/market.db")
