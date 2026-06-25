"""
Cross-market mean-reversion backtest (net of spreads).

Signal per game: D = P_draw_direct(1X2 draw mid) - P_draw_synth(sum of drawn
score mids: 0-0,1-1,2-2,3-3). Baseline = median(D) over the game (absorbs the
'Other' tail offset). An EVENT opens when |D - baseline| >= TAU_ENTER and closes
when it reverts below TAU_EXIT (or game ends).

The pair trade touches all legs (1X2 draw + the drawn scores). Round-trip you
cross each leg's spread once -> cost = sum(spread_leg). Per-share P&L:
   gross = entry_dev - exit_dev      (mid-gap convergence, prob units)
   net   = gross - sum(spread_leg)   (what's left after paying the spreads)
Reports whether the signal is net +EV, and how that depends on entry size.

Usage:  python3 tools/backtest_meanrev.py [db_path]
"""

import re
import sqlite3
import sys
from statistics import median


def is_draw(label):
    m = re.search(r"(\d+)\s*-\s*(\d+)", label)
    return bool(m) and m.group(1) == m.group(2)


TAU_ENTER, TAU_EXIT = 0.05, 0.01


def run(db):
    c = sqlite3.connect(db)
    slugs = [r[0] for r in c.execute("SELECT DISTINCT slug FROM ticks")]
    events = []   # (entry_dev, cost, gross, net, reverted)

    for slug in slugs:
        rows = c.execute(
            "SELECT ts,market,label,bid,ask FROM ticks WHERE slug=? AND side='yes' ORDER BY ts",
            (slug,)).fetchall()
        if not rows:
            continue
        draw_labels = sorted(set(l for _, m, l, *_ in rows if m == "score" and is_draw(l)))
        if not draw_labels:
            continue
        bid, ask = {}, {}
        samples = []
        for ts, mk, lb, b, a in rows:
            bid[(mk, lb)], ask[(mk, lb)] = b or 0.0, a or 0.0
            db_, da_ = bid.get(("1x2", "draw"), 0.0), ask.get(("1x2", "draw"), 0.0)
            if db_ <= 0 or da_ <= 0:
                continue
            mids, spreads, ok = [], [da_ - db_], True
            for l in draw_labels:
                bb, aa = bid.get(("score", l), 0.0), ask.get(("score", l), 0.0)
                if aa <= 0:
                    ok = False
                    break
                mids.append((bb + aa) / 2)
                spreads.append(aa - bb)
            if not ok:
                continue
            direct = (db_ + da_) / 2
            D = direct - sum(mids)
            samples.append((ts, D, sum(spreads)))
        if len(samples) < 20:
            continue
        baseline = median(s[1] for s in samples)

        in_ev = False
        e_dev = e_cost = 0.0
        for ts, D, sp in samples:
            dev = abs(D - baseline)
            if not in_ev and dev >= TAU_ENTER:
                in_ev, e_dev, e_cost = True, dev, sp
            elif in_ev and dev <= TAU_EXIT:
                gross = e_dev - dev
                events.append((e_dev, e_cost, gross, gross - e_cost, True))
                in_ev = False
        if in_ev:  # game ended mid-event
            last_dev = abs(samples[-1][1] - baseline)
            gross = e_dev - last_dev
            events.append((e_dev, e_cost, gross, gross - e_cost, False))

    if not events:
        print("无背离事件")
        return
    n = len(events)
    rev = sum(1 for e in events if e[4])
    avg = lambda i: sum(e[i] for e in events) / n
    net_pos = sum(1 for e in events if e[3] > 0)
    print(f"=== 跨市场均值回归回测 (平局: 直接 vs 波胆合成), 扣点差 ===")
    print(f"阈值: 进场|背离-基线|≥{TAU_ENTER*100:.0f}%, 出场≤{TAU_EXIT*100:.0f}% | {len(slugs)}场\n")
    print(f"背离事件总数: {n} | 成功回归: {rev} ({rev/n*100:.0f}%)")
    print(f"平均进场背离: {avg(0)*100:.1f}%  (毛收敛空间)")
    print(f"平均点差成本: {avg(1)*100:.1f}%  (5腿round-trip)")
    print(f"平均毛收敛  : {avg(2)*100:.1f}%")
    print(f"平均净EV/股 : {avg(3)*100:.2f}%  -> {'正EV✅' if avg(3)>0 else '负EV❌(点差吃光)'}")
    print(f"净为正的事件占比: {net_pos}/{n} = {net_pos/n*100:.0f}%\n")
    print("按进场背离分档 (净EV/股, ¢):")
    for lo, hi in [(0.05, 0.10), (0.10, 0.20), (0.20, 0.40), (0.40, 9)]:
        sub = [e for e in events if lo <= e[0] < hi]
        if sub:
            net = sum(e[3] for e in sub) / len(sub)
            cost = sum(e[1] for e in sub) / len(sub)
            print(f"  背离 {lo*100:.0f}-{hi*100:.0f}%: {len(sub)}次 | 平均成本{cost*100:.1f}¢ | 净EV {net*100:+.2f}¢/股 | 净正占比{sum(1 for e in sub if e[3]>0)/len(sub)*100:.0f}%")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "data/market.db")
