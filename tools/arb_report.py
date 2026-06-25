"""
Comprehensive arbitrage report on stored ticks -> Markdown, with a detailed
per-opportunity listing (each executable window: game, type, time, duration,
edge, executable $, and the top-of-book legs price x size at the peak instant).

executable = edge in [0.3%,5%] AND peak$ >= $1 AND duration >= 3s.
Polymarket charges 0 trading fee; numbers gross of gas/slippage, OPTIMISTIC.

Usage:  python3 tools/arb_report.py <db_path> <out_md>
"""

import re
import sqlite3
import sys
import time


def score_side(label):
    m = re.search(r"(\d+)\s*-\s*(\d+)", label)
    if not m:
        return None
    i, j = int(m.group(1)), int(m.group(2))
    return "home" if i > j else "draw" if i == j else "away"


def pctl(xs, p):
    if not xs:
        return 0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


class Arb:
    """Per game+type window tracker; keeps leg snapshot at the peak-$ instant."""
    def __init__(self):
        self.win = []
        self._o = None; self._pe = 0.0; self._pu = 0.0; self._det = None

    def step(self, ts, on, edge=0.0, usd=0.0, detail=None):
        if on:
            if self._o is None:
                self._o, self._pe, self._pu, self._det = ts, edge, usd, detail
            else:
                self._pe = max(self._pe, edge)
                if usd > self._pu:
                    self._pu, self._det = usd, detail
        elif self._o is not None:
            self.win.append({"start": self._o, "dur": ts - self._o,
                             "edge": self._pe, "usd": self._pu, "detail": self._det})
            self._o = None


FLOOR, CAP, MIN_USD, MIN_DUR = 0.003, 0.05, 1.0, 3
C = lambda p: f"{p*100:.0f}¢"          # price -> cents
Q = lambda q: f"{q/1000:.1f}k" if q >= 1000 else f"{q:.0f}"   # size short


def analyze(db):
    c = sqlite3.connect(db)
    slugs = [r[0] for r in c.execute("SELECT DISTINCT slug FROM ticks")]
    meta = {s: (h, a) for s, h, a in c.execute("SELECT slug,home,away FROM games")}
    ticks = c.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    allwin = []          # detailed executable windows (global)
    summary = {k: {"plaus": 0, "plaus_usd": 0.0, "exec": 0, "exec_usd": 0.0,
                   "extreme": 0, "durs": []} for k in ["1x2_back", "1x2_lay", "score_back", "score_lay"]}
    xm = {"n": 0, "sum": 0.0, "max": 0.0, "dur5": 0.0}

    for slug in slugs:
        rows = c.execute(
            "SELECT ts, market, label, side, bid, ask, bid_size, ask_size "
            "FROM ticks WHERE slug=? ORDER BY ts", (slug,)).fetchall()
        if not rows:
            continue
        labs = sorted(set(l for _, m, l, *_ in rows if m == "score"))
        ask, askq, bid, bidq = {}, {}, {}, {}
        arbs = {k: Arb() for k in summary}
        xo, xon = None, False
        for ts, mk, lb, sd, b, a, bq, aq in rows:
            ask[(mk, lb, sd)], askq[(mk, lb, sd)] = a or 0.0, aq or 0.0
            bid[(mk, lb, sd)], bidq[(mk, lb, sd)] = b or 0.0, bq or 0.0
            L = [("1x2", x, "yes") for x in ("home", "draw", "away")]
            a3 = [ask.get(k, 0.0) for k in L]
            if all(x > 0 for x in a3):
                s = sum(a3); mq = min(askq.get(k, 0.0) for k in L)
                det = f"主{C(a3[0])}×{Q(askq.get(L[0],0))} 平{C(a3[1])}×{Q(askq.get(L[1],0))} 客{C(a3[2])}×{Q(askq.get(L[2],0))} (买入和{s:.3f})"
                arbs["1x2_back"].step(ts, s < 1, 1 - s, max(0, 1 - s) * mq, det)
            b3 = [bid.get(k, 0.0) for k in L]
            if all(x > 0 for x in b3):
                s = sum(b3); mq = min(bidq.get(k, 0.0) for k in L)
                det = f"主{C(b3[0])}×{Q(bidq.get(L[0],0))} 平{C(b3[1])}×{Q(bidq.get(L[1],0))} 客{C(b3[2])}×{Q(bidq.get(L[2],0))} (卖出和{s:.3f})"
                arbs["1x2_lay"].step(ts, s > 1, s - 1, max(0, s - 1) * mq, det)
            SL = [("score", x, "yes") for x in labs]
            sa = [ask.get(k, 0.0) for k in SL]
            if labs and all(x > 0 for x in sa):
                s = sum(sa)
                qs = [(askq.get(k, 0.0), labs[i]) for i, k in enumerate(SL)]
                mq, ml = min(qs)
                det = f"17腿买入和{s:.3f}, 瓶颈腿 {ml} 仅{Q(mq)}量"
                arbs["score_back"].step(ts, s < 1, 1 - s, max(0, 1 - s) * mq, det)
            sb = [bid.get(k, 0.0) for k in SL]
            if labs and all(x > 0 for x in sb):
                s = sum(sb)
                qs = [(bidq.get(k, 0.0), labs[i]) for i, k in enumerate(SL)]
                mq, ml = min(qs)
                det = f"17腿卖出和{s:.3f}, 瓶颈腿 {ml} 仅{Q(mq)}量"
                arbs["score_lay"].step(ts, s > 1, s - 1, max(0, s - 1) * mq, det)
            db_, da_ = bid.get(("1x2", "draw", "yes"), 0.0), ask.get(("1x2", "draw", "yes"), 0.0)
            if db_ > 0 and da_ > 0 and labs:
                direct = (db_ + da_) / 2
                mids, ok = [], True
                for l in [x for x in labs if score_side(x) == "draw"]:
                    bb, aa = bid.get(("score", l, "yes"), 0.0), ask.get(("score", l, "yes"), 0.0)
                    if aa > 0:
                        mids.append((bb + aa) / 2)
                    else:
                        ok = False
                if ok and mids:
                    div = abs(direct - sum(mids))
                    xm["n"] += 1; xm["sum"] += div; xm["max"] = max(xm["max"], div)
                    on = div > 0.05
                    if on and not xon:
                        xo = ts
                    elif not on and xon and xo is not None:
                        xm["dur5"] += ts - xo
                    xon = on
        for k, arb in arbs.items():
            for w in arb.win:
                e, u, d = w["edge"], w["usd"], w["dur"]
                if e > CAP:
                    summary[k]["extreme"] += 1
                    continue
                if e < FLOOR:
                    continue
                summary[k]["plaus"] += 1; summary[k]["plaus_usd"] += u
                if u >= MIN_USD and d >= MIN_DUR:
                    summary[k]["exec"] += 1; summary[k]["exec_usd"] += u; summary[k]["durs"].append(d)
                    allwin.append({**w, "slug": slug, "type": k,
                                   "home": meta.get(slug, ("?", "?"))[0], "away": meta.get(slug, ("?", "?"))[1]})
    return slugs, meta, ticks, summary, xm, allwin


def md(db, out):
    slugs, meta, ticks, summary, xm, allwin = analyze(db)
    names = {"1x2_back": "1X2正套", "1x2_lay": "1X2反套", "score_back": "波胆正套", "score_lay": "波胆反套"}
    exec_total = sum(summary[k]["exec_usd"] for k in summary)
    all_durs = [d for k in summary for d in summary[k]["durs"]]

    L = ["# FootballPolyEdge — 套利可行性 + 套利点明细", "",
         f"- 生成(UTC): {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} · {len(slugs)} 场 · {ticks:,} ticks",
         "- 零手续费；未扣 gas/滑点；**乐观上限**(峰值价×顶档量×多腿同成)",
         f"- 口径: 合理 edge∈[{FLOOR*100:.1f}%,{CAP*100:.0f}%]；可执行=合理且≥${MIN_USD:.0f}且≥{MIN_DUR}s；极端(>{CAP*100:.0f}%)=陈旧盘假象,剔除",
         "",
         "## A) 汇总",
         "",
         "| 类型 | 合理窗口 | 合理$ | 可执行窗口 | 可执行$ | 时长 中位/p90 | 极端(剔) |",
         "|------|--------|------|----------|--------|------------|--------|"]
    for k in ["1x2_back", "1x2_lay", "score_back", "score_lay"]:
        s = summary[k]
        L.append(f"| {names[k]} | {s['plaus']} | ${s['plaus_usd']:,.0f} | **{s['exec']}** | "
                 f"**${s['exec_usd']:,.0f}** | {pctl(s['durs'],50)}s/{pctl(s['durs'],90)}s | {s['extreme']} |")
    L += ["", f"**可执行合计 ≈ ${exec_total:,.0f} / {len(slugs)}场 ; 全部可执行窗口时长 中位{pctl(all_durs,50)}s p90{pctl(all_durs,90)}s**", ""]

    avg = xm["sum"] / xm["n"] if xm["n"] else 0
    L += ["## B) 跨市场不一致(波胆隐含平局 vs 直接平局)",
          f"- 采样{xm['n']:,} · 平均背离{avg*100:.2f}% · 最大{xm['max']*100:.1f}% · >5%累计{xm['dur5']/3600:.1f}h",
          "- 非无风险(Other含长尾平局)，是统计套利/时滞信号。", ""]

    L += ["## C) 可执行套利点明细（逐笔）", "",
          "按可成交$降序。腿明细=顶档峰值时刻的 价格×挂单量。",
          "", "| # | 比赛 | 类型 | 开始(UTC) | 时长 | edge | 可成交$ | 顶档腿明细 |",
          "|---|------|------|----------|------|------|--------|-----------|"]
    allwin.sort(key=lambda w: w["usd"], reverse=True)
    for i, w in enumerate(allwin, 1):
        t = time.strftime("%m-%d %H:%M:%S", time.gmtime(w["start"]))
        dur = f"{w['dur']}s" if w["dur"] < 120 else f"{w['dur']//60}m"
        L.append(f"| {i} | {w['home']} vs {w['away']} | {names[w['type']]} | {t} | {dur} | "
                 f"+{w['edge']*100:.2f}% | ${w['usd']:,.1f} | {w['detail']} |")

    L += ["", "## 结论",
          "1. **钱在 1X2**（波胆盘太薄，可执行$可忽略）。",
          "2. 1X2 套利**可执行窗口中位时长够长(几十秒~分钟级)**，非微秒HFT，半自动/自动多腿脚本有机会抓。",
          "3. **$ 是乐观上限**：真实受抢单竞争、多腿同成风险、gas/滑点打折。",
          "4. 建议：先做 1X2 三腿自动执行器(paper)，验证真实成交率。", ""]
    open(out, "w", encoding="utf-8").write("\n".join(L))
    print(f"wrote {out} ; executable windows listed: {len(allwin)}")


if __name__ == "__main__":
    md(sys.argv[1] if len(sys.argv) > 1 else "data/market.db",
       sys.argv[2] if len(sys.argv) > 2 else "ARB_ANALYSIS.md")
