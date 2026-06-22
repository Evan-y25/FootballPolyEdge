"""
Auto-trader (自动交易) for the paper book — DEFAULT OFF.

A background loop that, when enabled, automatically opens and closes *paper*
positions using the same value signals shown in the UI. It never touches real
funds and never places real orders.

Rules (all configurable in config.py):
  OPEN  (upcoming games only):
    - opportunity is executable (size>0, real NO book), price >= AUTO_MIN_PRICE,
      edge >= AUTO_EDGE_THRESHOLD
    - stake = ¼ Kelly, capped by single-bet 20% and book liquidity
    - caps: <= AUTO_MAX_POSITIONS total, <= AUTO_MAX_PER_GAME per game,
      no duplicate (slug,market,label,side), total exposure <= AUTO_MAX_EXPOSURE
  CLOSE (any open position):
    - converged: best bid >= model fair  (edge consumed)
    - take-profit: unrealized >= +AUTO_TAKE_PROFIT
    - stop-loss:   unrealized <= -AUTO_STOP_LOSS
    - pre-kickoff: within AUTO_FORCE_CLOSE_MIN minutes of kickoff, or game gone
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict

from . import config, genome
from .paper import PaperTrader
from .state import AppState

logger = logging.getLogger(__name__)


def kelly_stake(edge: float, price: float, size: float, bankroll: float) -> float:
    """¼ Kelly, capped at 20% of bankroll and by book liquidity (size*price)."""
    if not (price > 0) or price >= 1:
        return 0.0
    full = edge / (1.0 - price)
    stake = bankroll * 0.25 * full
    stake = min(stake, bankroll * 0.2)
    liq = size * price
    if liq > 0:
        stake = min(stake, liq)
    return max(0.0, stake)


class AutoTrader:
    def __init__(self, state: AppState, paper: PaperTrader) -> None:
        self.state = state
        self.paper = paper
        self.enabled = False
        self.last_run = 0
        self.log: Deque[dict] = deque(maxlen=40)
        self._cooldown: Dict[tuple, int] = {}  # (slug,market,label,side) -> closed_ts
        # The genome IS the params: loaded from the committable genome.json file.
        self.params: Dict = genome.load(config.GENOME_PATH)
        self.paper.start_cash = self.params["bankroll"]

    def _p(self, key: str):
        return self.params[key]

    def set_params(self, updates: dict) -> dict:
        applied = {}
        for key, raw in (updates or {}).items():
            cv = genome.coerce(key, raw)
            if cv is None:
                continue
            self.params[key] = cv
            applied[key] = cv
            if key == "bankroll":
                self.paper.start_cash = cv  # bankroll IS the paper account size
        if applied:
            genome.save(config.GENOME_PATH, self.params)  # persist (committable)
            self._note("system", "参数更新: " + ", ".join(f"{k}={v}" for k, v in applied.items()))
        return self.status()

    # ---- control ----
    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "last_run": self.last_run,
            "params": {**self.params, "interval": config.AUTO_INTERVAL},
            "spec": {k: v[0] for k, v in genome.GENOME_SPEC.items()},
            "log": list(self.log),
        }

    def set_enabled(self, on: bool) -> dict:
        self.enabled = bool(on)
        self._note("system", f"自动交易 {'开启' if self.enabled else '关闭'}")
        logger.info("AutoTrader %s", "ENABLED" if self.enabled else "DISABLED")
        return self.status()

    def _note(self, kind: str, desc: str, pnl=None) -> None:
        self.log.appendleft({"ts": int(time.time()), "kind": kind, "desc": desc, "pnl": pnl})

    # ---- main loop ----
    async def run(self) -> None:
        while True:
            await asyncio.sleep(config.AUTO_INTERVAL)
            if not self.enabled:
                continue
            try:
                self._cycle()
                self.last_run = int(time.time())
            except Exception as exc:  # noqa: BLE001
                logger.warning("autotrader cycle error: %s", exc)

    def _cycle(self) -> None:
        self._close_pass()
        self._addon_pass()
        self._open_pass()

    # ---- helpers ----
    def _qualifying_edges(self) -> Dict[tuple, dict]:
        """All value edges that currently pass the open filters, keyed by (slug,label,side)."""
        qual: Dict[tuple, dict] = {}
        for g in self.state.snapshot()["games"]:
            sm = g.get("score_model")
            if not sm:
                continue
            for e in sm["value_edges"]:
                if e["size"] <= 0 or e.get("no_book") is False:
                    continue
                if e["price"] < self.params["min_price"] or e["edge"] < self.params["edge_threshold"]:
                    continue
                sp = e.get("spread_pct")
                if sp is None or sp > self.params["max_spread"]:
                    continue
                upcoming = g["status"] == "upcoming"
                side = "yes" if e["side"] == "buy_yes" else "no"
                if not self._direction_ok(side):
                    continue
                qual[(g["slug"], e["label"], side)] = {**e, "home": g["home"], "away": g["away"], "upcoming": upcoming}
        return qual

    def _direction_ok(self, side: str) -> bool:
        d = self.params.get("direction", "both")
        if d == "no_only":
            return side == "no"
        if d == "yes_only":
            return side == "yes"
        return True

    # ---- average-down (补仓，仅一次) ----
    def _addon_pass(self) -> None:
        if not self.params.get("addon_enabled", True):
            return
        snap = self.paper.snapshot()
        opens = snap["open"]
        exposure = sum(p["stake"] for p in opens)
        budget = self.paper.start_cash * self.params["max_exposure"] - exposure
        if budget < config.AUTO_MIN_STAKE:
            return
        qual = self._qualifying_edges()
        pre_only = self.params.get("addon_pre_match_only", True)
        for p in opens:
            if p.get("added"):
                continue  # only once per position
            e = qual.get((p["slug"], p["label"], p["side"]))
            if not e:
                continue  # no longer meets entry conditions at current price
            if pre_only and not e.get("upcoming", False):
                continue  # don't average down into a live (likely-real) move
            entry = p["entry_price"]
            if entry <= 0:
                continue
            drop = (entry - e["price"]) / entry  # current ask fell vs our cost
            if drop < self.params["add_drop"]:
                continue
            stake = min(kelly_stake(e["edge"], e["price"], e["size"], self.paper.start_cash), budget)
            if stake < config.AUTO_MIN_STAKE:
                continue
            res = self.paper.add_to(p["id"], round(stake, 2))
            if res.get("ok"):
                pos = res["position"]
                self._note("add", f"#{pos['id']} {e['home']} {p['label']} {p['side'].upper()} 补仓@{e['price']:.3f}(跌{drop*100:.0f}%) 价值+{e['edge']*100:.1f}% +${stake:.2f} 均价{pos['entry_price']:.3f}")
                exposure += stake
                budget = self.paper.start_cash * self.params["max_exposure"] - exposure

    # ---- close ----
    def _close_pass(self) -> None:
        now = datetime.now(timezone.utc)
        snap = self.paper.snapshot()
        for p in snap["open"]:
            reason = self._close_reason(p, now)
            if not reason:
                continue
            res = self.paper.close(p["id"], reason=reason)
            if res.get("ok"):
                pos = res["position"]
                # Block immediate re-entry of the same instrument.
                self._cooldown[(pos["slug"], pos["market"], pos["label"], pos["side"])] = int(time.time())
                self._note("close", f"#{pos['id']} {pos['home']} {pos['market']}:{pos['label']} {pos['side'].upper()} [{reason}]", pos["realized_pnl"])

    def _is_hold_to_settle(self, p: dict) -> bool:
        """High-price 'buy NO' longshot-fade -> hold to settlement."""
        return p["side"] == "no" and p["entry_price"] >= self.params["hold_to_settle_price"]

    def _close_reason(self, p: dict, now: datetime):
        game = self.state.find_game(p["slug"])
        # Game left the feed (closed/resolved) -> settle at last bid (both modes).
        if game is None:
            return "settle"
        pct = p["unrealized_pct"] / 100.0
        # Stop-loss applies in BOTH modes (disaster protection) — if enabled.
        if self.params.get("stop_loss_enabled", True) and pct <= -self.params["stop_loss"]:
            return "stop-loss"
        if self._is_hold_to_settle(p):
            return None  # ride to settlement; only stop-loss / settle exit
        # Convergence mode (everything else):
        ko = game.kickoff_dt()
        if ko is not None and (ko - now).total_seconds() / 60.0 <= self.params["force_close_min"]:
            return "pre-kickoff"
        if p["signal"] == "converged":
            return "converged"
        if pct >= self.params["take_profit"]:
            return "take-profit"
        return None

    # ---- open ----
    def _open_pass(self) -> None:
        snap = self.paper.snapshot()
        open_positions = snap["open"]
        if len(open_positions) >= self.params["max_positions"]:
            return

        exposure = sum(p["stake"] for p in open_positions)
        cap_usd = self.paper.start_cash * self.params["max_exposure"]
        budget = cap_usd - exposure
        if budget < config.AUTO_MIN_STAKE:
            return

        per_game: Dict[str, int] = {}
        held = set()
        for p in open_positions:
            per_game[p["slug"]] = per_game.get(p["slug"], 0) + 1
            held.add((p["slug"], p["market"], p["label"], p["side"]))

        # Gather candidate opportunities from the live snapshot.
        candidates = []
        for g in self.state.snapshot()["games"]:
            if g["status"] != "upcoming":
                continue
            sm = g.get("score_model")
            if not sm:
                continue
            for e in sm["value_edges"]:
                if e["size"] <= 0 or e.get("no_book") is False:
                    continue
                if e["price"] < self.params["min_price"] or e["edge"] < self.params["edge_threshold"]:
                    continue
                # Spread guard: skip wide-spread "mirage" edges (unrealizable, instant stop-out).
                sp = e.get("spread_pct")
                if sp is None or sp > self.params["max_spread"]:
                    continue
                side = "yes" if e["side"] == "buy_yes" else "no"
                if not self._direction_ok(side):
                    continue
                candidates.append((e["edge"], g["slug"], g["home"], g["away"], e, side))

        candidates.sort(key=lambda c: c[0], reverse=True)

        now_ts = int(time.time())
        n_open = len(open_positions)
        for edge, slug, home, away, e, side in candidates:
            if n_open >= self.params["max_positions"] or budget < config.AUTO_MIN_STAKE:
                break
            if per_game.get(slug, 0) >= self.params["max_per_game"]:
                continue
            key = (slug, "score", e["label"], side)
            if key in held:
                continue
            # Re-entry cooldown after a recent close of the same instrument.
            closed_ts = self._cooldown.get(key)
            if closed_ts and now_ts - closed_ts < self.params["reentry_cooldown"]:
                continue
            stake = kelly_stake(e["edge"], e["price"], e["size"], self.paper.start_cash)
            stake = min(stake, budget)
            if stake < config.AUTO_MIN_STAKE:
                continue
            res = self.paper.open(slug, "score", e["label"], side, round(stake, 2))
            if res.get("ok"):
                pos = res["position"]
                self._note("open", f"#{pos['id']} {home} {e['label']} {side.upper()} @{pos['entry_price']:.3f} 价值+{e['edge']*100:.1f}% ${pos['stake']:.2f}")
                held.add(key)
                per_game[slug] = per_game.get(slug, 0) + 1
                exposure += pos["stake"]
                budget = cap_usd - exposure
                n_open += 1
