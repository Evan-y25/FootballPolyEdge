"""
Predictive-edge backtest (Brier + log-loss) on resolved games.

For each resolved game we take the PRE-MATCH closing snapshot (last quotes
before kickoff), then compare two probability distributions over the 17
exact-score buckets at predicting the ACTUAL final score:
  - model  : our Dixon-Coles fit derived from the (de-vigged) 1X2
  - market : the (de-vigged) exact-score book itself

Lower Brier / log-loss = more accurate. If model < market, we have a genuine
predictive edge (taker +EV possible). If not, no forecasting edge -> structural
strategies (market-making / mean-reversion) only.

Usage:  python3 tools/backtest_brier.py [db_path]
"""

import math
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from app import score_model  # noqa: E402


def kickoff_ts(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def brier(dist, actual):
    return sum((p - (1.0 if lab == actual else 0.0)) ** 2 for lab, p in dist.items())


def logloss(dist, actual):
    p = max(1e-6, dist.get(actual, 0.0))
    return -math.log(p)


def run(db):
    c = sqlite3.connect(db)
    games = c.execute("SELECT slug, home, away, kickoff FROM games").fetchall()
    resolved = set(r[0] for r in c.execute("SELECT DISTINCT slug FROM resolutions"))

    rows = []
    for slug, home, away, kickoff in games:
        if slug not in resolved:
            continue
        ko = kickoff_ts(kickoff)
        if not ko:
            continue
        # actual winning score bucket
        win = c.execute(
            "SELECT label FROM resolutions WHERE slug=? AND market='score' AND winner='yes'", (slug,)
        ).fetchone()
        if not win:
            continue
        actual = win[0]
        # pre-match closing snapshot (last yes quote per market/label before kickoff)
        snap = {}
        for ts, market, label, b, a in c.execute(
            "SELECT ts,market,label,bid,ask FROM ticks WHERE slug=? AND side='yes' AND ts<=? ORDER BY ts",
            (slug, ko)):
            snap[(market, label)] = (b or 0.0, a or 0.0)
        if not snap:
            continue
        # 1X2 mids
        def mid(key):
            v = snap.get(key)
            if not v:
                return 0.0
            b, a = v
            return (b + a) / 2 if (b or a) else 0.0
        oh, od, oa = mid(("1x2", "home")), mid(("1x2", "draw")), mid(("1x2", "away"))
        if min(oh, od, oa) <= 0:
            continue
        score_quotes = [{"label": l, "bid": snap[(m, l)][0], "ask": snap[(m, l)][1],
                         "bid_size": 1, "ask_size": 1}
                        for (m, l) in snap if m == "score"]
        if len(score_quotes) < 5:
            continue
        sm = score_model.build_score_model((oh, od, oa), score_quotes, threshold=1.0)
        if not sm:
            continue
        model = {lab: cell["model"] for lab, cell in sm["matrix"].items()}
        market = {lab: cell["market"] for lab, cell in sm["matrix"].items()}
        if actual not in model:
            continue
        rows.append((slug, home, away, actual,
                     brier(model, actual), brier(market, actual),
                     logloss(model, actual), logloss(market, actual),
                     model.get(actual, 0), market.get(actual, 0)))

    if not rows:
        print("no usable resolved games with pre-match data")
        return

    n = len(rows)
    bm = sum(r[4] for r in rows) / n
    bk = sum(r[5] for r in rows) / n
    lm = sum(r[6] for r in rows) / n
    lk = sum(r[7] for r in rows) / n
    model_wins = sum(1 for r in rows if r[4] < r[5])

    print(f"=== 预测性 edge 回测 (赛前收盘快照, {n} 场已结算) ===\n")
    print(f"{'比赛':32s} {'终场':7s} {'模型P':>7s} {'市场P':>7s}  {'Brier模/市':>14s}")
    for slug, h, a, act, bmo, bma, lmo, lma, pm, pk in sorted(rows, key=lambda r: r[4] - r[5]):
        nm = f"{h} vs {a}"[:30]
        print(f"{nm:32s} {act:7s} {pm*100:6.1f}% {pk*100:6.1f}%  {bmo:6.3f}/{bma:6.3f}")
    print(f"\n--- 平均 (越低越准) ---")
    print(f"Brier   : 模型 {bm:.4f}  vs  市场 {bk:.4f}   -> {'模型更准✅' if bm<bk else '市场更准❌'}")
    print(f"LogLoss : 模型 {lm:.4f}  vs  市场 {lk:.4f}   -> {'模型更准✅' if lm<lk else '市场更准❌'}")
    print(f"模型单场更准的比例: {model_wins}/{n} = {model_wins/n*100:.0f}%")
    print()
    if bm < bk and lm < lk:
        print("结论: 模型在预测真实比分上优于市场 -> 存在预测性 edge, taker 方向下注可能正EV(需扣点差验证)。")
    else:
        print("结论: 模型不优于市场 -> 无预测性 edge。方向性吃单会被点差吃掉, 应走结构性策略(做市/均值回归)。")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "data/market.db")
