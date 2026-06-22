"""
Backtest engine (Phase 1 — scaffold).

Replays a resolved game's stored ticks under a given genome and scores the
strategy by settling at the TRUE outcome. This first version backtests the
*entry selection* (pick qualifying value edges at the earliest observed/pre-match
snapshot, size by ¼ Kelly under the caps, settle at resolution). Intra-match
dynamics (stop-loss / averaging-down) are a documented TODO for when enough
high-frequency tick history has accumulated.

It will become the evolution gate ("a proposed genome must beat the current one
on history") once there is enough data — until then `evolve.py` uses heuristics.
"""

from __future__ import annotations

from typing import Dict, List

from . import config, score_model


def _kelly(edge: float, price: float, size: float, bankroll: float) -> float:
    if not (price > 0) or price >= 1:
        return 0.0
    full = edge / (1.0 - price)
    stake = min(bankroll * 0.25 * full, bankroll * 0.2)
    liq = size * price
    if liq > 0:
        stake = min(stake, liq)
    return max(0.0, stake)


def _earliest_quotes(ticks: List[dict]) -> Dict[str, dict]:
    """First observed quote per (market,label) with yes/no sides merged."""
    q: Dict[str, dict] = {}
    seen = set()
    for t in ticks:  # ticks come ts-ordered
        key = f"{t['market']}|{t['label']}"
        sub = (key, t["side"])
        if sub in seen:
            continue
        seen.add(sub)
        d = q.setdefault(key, {"market": t["market"], "label": t["label"]})
        if t["side"] == "yes":
            d.update(bid=t["bid"], ask=t["ask"], bid_size=t["bid_size"], ask_size=t["ask_size"])
        else:
            d.update(no_bid=t["bid"], no_ask=t["ask"], no_bid_size=t["bid_size"], no_ask_size=t["ask_size"])
    return q


def backtest_game(store, slug: str, params: dict) -> dict:
    """Return {pnl, n_trades, wins, invested} for one resolved game under `params`."""
    ticks = store.ticks_for_slug(slug)
    winners = store.resolution_map(slug)
    if not ticks or not winners:
        return {"pnl": 0.0, "n_trades": 0, "wins": 0, "invested": 0.0, "skipped": True}

    q = _earliest_quotes(ticks)
    onex2 = {}
    score_quotes = []
    for key, d in q.items():
        if d["market"] == "1x2":
            onex2[d["label"]] = d
        else:
            score_quotes.append(d)

    def mid(d):
        b, a = d.get("bid") or 0, d.get("ask") or 0
        return (b + a) / 2 if (b or a) else 0.0

    if not score_quotes or any(mid(onex2.get(k, {})) <= 0 for k in ("home", "draw", "away")):
        return {"pnl": 0.0, "n_trades": 0, "wins": 0, "invested": 0.0, "skipped": True}

    sm = score_model.build_score_model(
        (mid(onex2["home"]), mid(onex2["draw"]), mid(onex2["away"])),
        score_quotes,
        threshold=params.get("edge_threshold", 0.02),
    )
    if not sm:
        return {"pnl": 0.0, "n_trades": 0, "wins": 0, "invested": 0.0, "skipped": True}

    direction = params.get("direction", "both")
    bankroll = params.get("bankroll", 100.0)
    cap = bankroll * params.get("max_exposure", 0.95)
    edges = sorted(sm["value_edges"], key=lambda e: e["edge"], reverse=True)

    pnl = invested = 0.0
    n = wins = 0
    per_game = 0
    for e in edges:
        if e["size"] <= 0 or e.get("no_book") is False:
            continue
        if e["price"] < params.get("min_price", 0.5) or e["edge"] < params.get("edge_threshold", 0.03):
            continue
        sp = e.get("spread_pct")
        if sp is None or sp > params.get("max_spread", 0.08):
            continue
        side = "yes" if e["side"] == "buy_yes" else "no"
        if direction == "no_only" and side != "no":
            continue
        if direction == "yes_only" and side != "yes":
            continue
        if n >= params.get("max_positions", 20) or per_game >= params.get("max_per_game", 3):
            continue
        stake = _kelly(e["edge"], e["price"], e["size"], bankroll)
        stake = min(stake, cap - invested)
        if stake < config.AUTO_MIN_STAKE:
            continue
        winner = winners.get(("score", e["label"]))
        if winner not in ("yes", "no"):
            continue
        shares = stake / e["price"]
        value = 1.0 if side == winner else 0.0
        trade_pnl = shares * value - stake
        pnl += trade_pnl
        invested += stake
        n += 1
        per_game += 1
        if trade_pnl > 0:
            wins += 1

    return {"pnl": round(pnl, 2), "n_trades": n, "wins": wins,
            "invested": round(invested, 2), "skipped": False}


def backtest_period(store, slugs: List[str], params: dict) -> dict:
    tot_pnl = tot_inv = 0.0
    n = wins = games = 0
    for slug in slugs:
        r = backtest_game(store, slug, params)
        if r.get("skipped"):
            continue
        games += 1
        tot_pnl += r["pnl"]
        tot_inv += r["invested"]
        n += r["n_trades"]
        wins += r["wins"]
    roi = (tot_pnl / tot_inv) if tot_inv else 0.0
    return {"games": games, "n_trades": n, "wins": wins,
            "win_rate": round(wins / n, 3) if n else 0.0,
            "pnl": round(tot_pnl, 2), "invested": round(tot_inv, 2), "roi": round(roi, 4)}
