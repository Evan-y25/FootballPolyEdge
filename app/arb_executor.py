"""
1X2 three-leg risk-free arbitrage executor (paper).

Both directions are pure BUY baskets (executable, risk-free, hold to settlement):
  back: buy YES on home+draw+away.  Exactly 1 wins -> $1 payout per set.
        cost = sum(ask_yes).  profit/set = 1 - sum(ask_yes).   trigger sum<1.
  lay : buy NO  on home+draw+away.  Exactly 2 win  -> $2 payout per set.
        cost = sum(ask_no).   profit/set = 2 - sum(ask_no).    trigger sum<2.

Equal shares across legs => fixed payout per set => locked profit. Uses a
dedicated paper book (separate from the value auto-trader) so arb P&L is clean.
Settlement is handled by paper.settle() in the resolution loop. Paper mode
validates detection + theoretical capture; real fill-rate needs live orders.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Deque, List

from . import config

logger = logging.getLogger(__name__)


class ArbExecutor:
    def __init__(self, state, paper) -> None:
        self.state = state
        self.paper = paper          # dedicated arb paper book
        self.enabled = False
        self.last_run = 0
        self.baskets: List[dict] = []   # {slug,kind,legs:[ids],edge,cost,sets,profit,ts}
        self.log: Deque[dict] = deque(maxlen=40)

    # ---- control ----
    def set_enabled(self, on: bool) -> dict:
        self.enabled = bool(on)
        self._note(f"套利执行器 {'开启' if self.enabled else '关闭'}")
        return self.status()

    def _note(self, desc: str) -> None:
        self.log.appendleft({"ts": int(time.time()), "desc": desc})

    # ---- scan + execute ----
    def scan_once(self) -> dict:
        opened = 0
        # baskets still open (any leg not yet settled) -> dedup per (slug,kind)
        open_ids = {p["id"] for p in self.paper.positions if p["status"] == "open"}
        held = {(b["slug"], b["kind"]) for b in self.baskets
                if any(i in open_ids for i in b["legs"])}

        exposure = sum(p["stake"] for p in self.paper.positions if p["status"] == "open")
        budget = self.paper.start_cash * config.ARB_MAX_EXPOSURE - exposure

        for g in self.state.games:
            legs = [g.home_win, g.draw, g.away_win]
            if not all(legs):
                continue
            # ---- back: buy YES x3, sum(ask_yes) < 1 ----
            if (g.slug, "back") not in held:
                q = [self.state.token_best(o.yes_token) for o in legs]
                asks = [x["ask"] for x in q]
                if all(a > 0 for a in asks):
                    cost = sum(asks)
                    if cost < 1.0 - config.ARB_MIN_EDGE:
                        opened += self._exec(g, "back", legs, "yes", asks,
                                             [x["ask_size"] for x in q], 1.0 - cost, budget)
                        budget = self.paper.start_cash * config.ARB_MAX_EXPOSURE - \
                            sum(p["stake"] for p in self.paper.positions if p["status"] == "open")
            # ---- lay: buy NO x3, sum(ask_no) < 2 ----
            if (g.slug, "lay") not in held:
                q = [self.state.token_best(o.no_token) for o in legs]
                asks = [x["ask"] for x in q]
                if all(a > 0 for a in asks):
                    cost = sum(asks)
                    if cost < 2.0 - config.ARB_MIN_EDGE:
                        opened += self._exec(g, "lay", legs, "no", asks,
                                             [x["ask_size"] for x in q], 2.0 - cost, budget)
                        budget = self.paper.start_cash * config.ARB_MAX_EXPOSURE - \
                            sum(p["stake"] for p in self.paper.positions if p["status"] == "open")
        self.last_run = int(time.time())
        return {"opened": opened}

    def _exec(self, game, kind, legs, side, prices, sizes, profit_per_set, budget) -> int:
        sets = min(sizes)
        if sets <= 0:
            return 0
        capital = sets * sum(prices)
        # cap by per-arb max stake and remaining exposure budget
        cap = min(config.ARB_MAX_STAKE, max(0.0, budget))
        if capital > cap:
            if cap < 1:
                return 0
            sets *= cap / capital
            capital = sets * sum(prices)
        if sets * profit_per_set < config.ARB_MIN_PROFIT:
            return 0
        leg_ids = []
        labels = ["home", "draw", "away"]
        for lbl, price in zip(labels, prices):
            stake = round(sets * price, 2)
            if stake <= 0:
                continue
            res = self.paper.open(game.slug, "1x2", lbl, side, stake)
            if res.get("ok"):
                leg_ids.append(res["position"]["id"])
        if len(leg_ids) < 3:
            return 0  # partial — in paper this shouldn't happen, but guard
        profit = round(sets * profit_per_set, 2)
        self.baskets.append({"slug": game.slug, "home": game.home, "away": game.away,
                             "kind": kind, "legs": leg_ids, "edge": round(profit_per_set, 4),
                             "cost": round(capital, 2), "sets": round(sets, 1),
                             "profit": profit, "ts": int(time.time())})
        self._note(f"{game.home} vs {game.away} [{kind}] 锁定+${profit:.2f} (edge {profit_per_set*100:.2f}%, 成本${capital:.0f})")
        logger.info("ARB %s %s edge=%.3f profit=$%.2f", game.slug, kind, profit_per_set, profit)
        return 1

    async def run(self) -> None:
        import asyncio
        while True:
            await asyncio.sleep(config.ARB_INTERVAL)
            if not self.enabled:
                continue
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("arb scan error: %s", exc)

    # ---- status ----
    def status(self) -> dict:
        snap = self.paper.snapshot()
        pos_status = {p["id"]: p["status"] for p in self.paper.positions}
        pnl_by = {}
        for p in self.paper.positions:
            pnl_by[p["id"]] = p.get("realized_pnl")
        out_baskets = []
        for b in self.baskets[-60:]:
            settled = all(pos_status.get(i) == "closed" for i in b["legs"])
            realized = sum((pnl_by.get(i) or 0.0) for i in b["legs"]) if settled else None
            out_baskets.append({**b, "settled": settled,
                                "realized": round(realized, 2) if realized is not None else None})
        out_baskets.reverse()
        return {
            "enabled": self.enabled,
            "last_run": self.last_run,
            "params": {"min_edge": config.ARB_MIN_EDGE, "min_profit": config.ARB_MIN_PROFIT,
                       "max_stake": config.ARB_MAX_STAKE, "interval": config.ARB_INTERVAL,
                       "bankroll": self.paper.start_cash},
            "account": {"start": snap["start_cash"], "equity": snap["equity"],
                        "realized": snap["realized_pnl"], "unrealized": snap["unrealized_pnl"],
                        "open": snap["open_count"]},
            "baskets": out_baskets,
            "log": list(self.log),
        }
