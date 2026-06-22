"""
Paper-trading engine (模拟盘).

Simulates buying/selling outcome tokens against the *live* Polymarket orderbook
(no real money, no real orders). Tracks open positions with floating P&L and a
"value converged" exit signal derived from the score model, plus a realized
ledger. Persists to a JSON file so it survives restarts.

Fill model (optimistic, top-of-book; ignores fees/slippage/partial fills):
  - open  -> buy at current best ask  (shares = stake / ask)
  - close -> sell at current best bid  (proceeds = shares * bid)

Equity = start_cash + realized_pnl + unrealized_pnl.
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from typing import List, Optional

from . import config
from .state import AppState

logger = logging.getLogger(__name__)


class PaperTrader:
    def __init__(self, state: AppState, path: pathlib.Path, start_cash: float) -> None:
        self.state = state
        self.path = path
        self.start_cash = start_cash
        self.positions: List[dict] = []
        self._seq = 0
        self._lock = threading.Lock()
        self.auto = None  # set by main; provides live params for exit signals
        self.store = None  # set by main; journals trades for replay
        self._load()

    def _journal(self, pos: dict) -> None:
        if self.store is not None:
            try:
                self.store.record_trade(pos)
            except Exception as exc:  # noqa: BLE001
                logger.warning("trade journal failed: %s", exc)

    def _param(self, key: str, default: float) -> float:
        if self.auto is not None:
            return self.auto.params.get(key, default)
        return default

    # ---- persistence -----------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            self.positions = data.get("positions", [])
            self._seq = data.get("seq", len(self.positions))
        except (FileNotFoundError, json.JSONDecodeError):
            self.positions = []
            self._seq = 0

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps({"positions": self.positions, "seq": self._seq}, ensure_ascii=False, indent=2)
            )
        except OSError as exc:
            logger.warning("paper save failed: %s", exc)

    # ---- actions ---------------------------------------------------------
    def open(self, slug: str, market: str, label: str, side: str, stake: float) -> dict:
        market = market if market in ("score", "1x2") else "score"
        side = "no" if side == "no" else "yes"
        if not (stake and stake > 0):
            return {"ok": False, "error": "下注金额需 > 0"}
        game = self.state.find_game(slug)
        if not game:
            return {"ok": False, "error": "比赛不存在或已结束"}
        outcome = self.state.find_outcome(game, market, label)
        if not outcome:
            return {"ok": False, "error": f"找不到标的 {market}:{label}"}
        token = outcome.yes_token if side == "yes" else outcome.no_token
        if not token:
            return {"ok": False, "error": "该方向无 token"}
        book = self.state.token_best(token)
        ask = book["ask"]
        if not (ask and ask > 0):
            return {"ok": False, "error": "无卖单，无法成交"}
        shares = stake / ask
        with self._lock:
            self._seq += 1
            pos = {
                "id": self._seq,
                "slug": slug,
                "home": game.home,
                "away": game.away,
                "market": market,
                "label": label,
                "side": side,           # held token: yes / no
                "token": token,
                "entry_price": round(ask, 4),
                "shares": round(shares, 4),
                "stake": round(stake, 2),
                "opened_at": int(time.time()),
                "status": "open",
                "added": False,
                "close_price": None,
                "proceeds": None,
                "realized_pnl": None,
                "closed_at": None,
            }
            self.positions.append(pos)
            self._save()
        logger.info("PAPER open #%d %s %s:%s %s $%.2f @ %.3f", pos["id"], slug, market, label, side, stake, ask)
        self._journal(pos)
        return {"ok": True, "position": pos}

    def add_to(self, pos_id: int, stake: float) -> dict:
        """Average down: buy more of an existing position at the current ask. Once only."""
        if not (stake and stake > 0):
            return {"ok": False, "error": "补仓金额需 > 0"}
        with self._lock:
            pos = next((p for p in self.positions if p["id"] == pos_id and p["status"] == "open"), None)
            if not pos:
                return {"ok": False, "error": "持仓不存在或已平仓"}
            if pos.get("added"):
                return {"ok": False, "error": "已补过仓（仅一次）"}
            book = self.state.token_best(pos["token"])
            ask = book["ask"]
            if not (ask and ask > 0):
                return {"ok": False, "error": "无卖单，无法补仓"}
            add_shares = stake / ask
            new_shares = pos["shares"] + add_shares
            new_stake = pos["stake"] + stake
            pos["entry_price"] = round(new_stake / new_shares, 4)  # weighted-avg cost
            pos["shares"] = round(new_shares, 4)
            pos["stake"] = round(new_stake, 2)
            pos["added"] = True
            pos["add_price"] = round(ask, 4)
            pos["add_stake"] = round(stake, 2)
            self._save()
        logger.info("PAPER add #%d +$%.2f @ %.3f avg=%.3f", pos_id, stake, ask, pos["entry_price"])
        self._journal(pos)
        return {"ok": True, "position": pos}

    def close(self, pos_id: int, reason: str = "manual") -> dict:
        with self._lock:
            pos = next((p for p in self.positions if p["id"] == pos_id and p["status"] == "open"), None)
            if not pos:
                return {"ok": False, "error": "持仓不存在或已平仓"}
            book = self.state.token_best(pos["token"])
            bid = book["bid"]
            if not (bid and bid > 0):
                return {"ok": False, "error": "无买单，暂时无法平仓"}
            proceeds = pos["shares"] * bid
            pos["status"] = "closed"
            pos["close_price"] = round(bid, 4)
            pos["proceeds"] = round(proceeds, 2)
            pos["realized_pnl"] = round(proceeds - pos["stake"], 2)
            pos["closed_at"] = int(time.time())
            pos["close_reason"] = reason
            self._save()
        logger.info("PAPER close #%d @ %.3f pnl=%.2f", pos_id, bid, pos["realized_pnl"])
        self._journal(pos)
        return {"ok": True, "position": pos}

    def settle(self, slug: str, winners: dict) -> list:
        """
        Settle open positions on a resolved game at the TRUE outcome (1.0 / 0.0),
        not a stale bid. winners: (market,label) -> 'yes'|'no'. Returns settled positions.
        """
        settled = []
        with self._lock:
            for pos in self.positions:
                if pos["status"] != "open" or pos["slug"] != slug:
                    continue
                winner = winners.get((pos["market"], pos["label"]))
                if winner not in ("yes", "no"):
                    continue
                value = 1.0 if pos["side"] == winner else 0.0
                proceeds = pos["shares"] * value
                pos["status"] = "closed"
                pos["close_price"] = value
                pos["proceeds"] = round(proceeds, 2)
                pos["realized_pnl"] = round(proceeds - pos["stake"], 2)
                pos["closed_at"] = int(time.time())
                pos["close_reason"] = "settled"
                settled.append(pos)
            if settled:
                self._save()
        for pos in settled:
            self._journal(pos)
            logger.info("PAPER settle #%d %s %s=%s pnl=%.2f", pos["id"], pos["label"], pos["side"], value, pos["realized_pnl"])
        return settled

    def reset(self) -> dict:
        with self._lock:
            self.positions = []
            self._seq = 0
            self._save()
        return {"ok": True}

    # ---- marking ---------------------------------------------------------
    def _mark_open(self, pos: dict) -> dict:
        book = self.state.token_best(pos["token"])
        bid = book["bid"]
        mark_value = pos["shares"] * bid
        upnl = mark_value - pos["stake"]
        fair = self.state.fair_for(pos["slug"], pos["market"], pos["label"], pos["side"])
        # Exit signal — mirrors the auto-trader's hybrid logic.
        pnl_pct = (upnl / pos["stake"]) if pos["stake"] else 0.0
        hold_price = self._param("hold_to_settle_price", config.AUTO_HOLD_TO_SETTLE_PRICE)
        stop_loss = self._param("stop_loss", config.AUTO_STOP_LOSS)
        hold_to_settle = pos["side"] == "no" and pos["entry_price"] >= hold_price
        if pnl_pct <= -stop_loss:
            signal, hint = "stoploss", "触及止损"
        elif hold_to_settle:
            signal, hint = "settle", "持有至结算"
        elif fair is not None and bid > 0 and bid >= fair:
            signal, hint = "converged", "已收敛·建议平仓止盈"
        elif bid >= pos["entry_price"]:
            signal, hint = "profit", "浮盈·可止盈"
        else:
            signal, hint = "hold", "持有"
        return {
            **pos,
            "close_bid": round(bid, 4),
            "close_bid_size": round(book["bid_size"], 2),
            "fair": round(fair, 4) if fair is not None else None,
            "mark_value": round(mark_value, 2),
            "unrealized_pnl": round(upnl, 2),
            "unrealized_pct": round(pnl_pct * 100, 2),
            "signal": signal,
            "hint": hint,
        }

    def snapshot(self) -> dict:
        opens, closeds = [], []
        invested_open = realized = unrealized = 0.0
        for p in self.positions:
            if p["status"] == "open":
                m = self._mark_open(p)
                opens.append(m)
                invested_open += p["stake"]
                unrealized += m["unrealized_pnl"]
            else:
                closeds.append(p)
                realized += p["realized_pnl"] or 0.0
        cash = self.start_cash - invested_open + realized
        equity = self.start_cash + realized + unrealized
        opens.sort(key=lambda x: x["id"], reverse=True)
        closeds.sort(key=lambda x: x.get("closed_at", 0), reverse=True)
        return {
            "start_cash": round(self.start_cash, 2),
            "cash": round(cash, 2),
            "equity": round(equity, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl": round(realized + unrealized, 2),
            "open_count": len(opens),
            "open": opens,
            "closed": closeds,
        }
