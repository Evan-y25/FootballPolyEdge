"""
Global application state: games registry, orderbook view, edge calculation,
and JSON serialization for the frontend.

Edge logic (see DESIGN.md §6). Only *well-defined, risk-free* edges are flagged:
  - 1X2 back arb : ask(home)+ask(draw)+ask(away) < 1
  - 1X2 lay  arb : bid(home)+bid(draw)+bid(away) > 1
  - Score back arb: sum of all 17 score asks < 1   (scores are exhaustive incl. "Other")
  - Score lay  arb: sum of all 17 score bids > 1
The 1X2/score overround is also surfaced as an informational number.
All figures ignore fees, gas and slippage and assume simultaneous fills.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import config, score_model
from .gamma import Game, Outcome
from .ws_client import MarketWebSocket


class AppState:
    def __init__(self, ws: MarketWebSocket) -> None:
        self.ws = ws
        self.games: List[Game] = []
        self.updated_at: float = 0.0

    def set_games(self, games: List[Game]) -> None:
        self.games = games
        self.updated_at = time.time()

    # ---- quote helpers ---------------------------------------------------
    def _quote(self, outcome: Optional[Outcome]) -> Optional[dict]:
        if outcome is None:
            return None
        bid = ask = bid_size = ask_size = 0.0
        ob = self.ws.get_orderbook(outcome.yes_token)
        if ob is not None:
            bid, bid_size = ob.best_bid_level()
            ask, ask_size = ob.best_ask_level()
        # Fallback to Gamma initial price if no live book yet.
        if bid == 0.0 and ask == 0.0:
            bid = ask = round(outcome.init_yes, 4)

        # NO-side book (the complementary token). "卖出 YES" 的等价执行是 "买入 NO"。
        no_bid = no_ask = no_bid_size = no_ask_size = 0.0
        ob_no = self.ws.get_orderbook(outcome.no_token) if outcome.no_token else None
        if ob_no is not None:
            no_bid, no_bid_size = ob_no.best_bid_level()
            no_ask, no_ask_size = ob_no.best_ask_level()

        return {
            "label": outcome.label,
            "question": outcome.question,
            "bid": round(bid, 4),
            "ask": round(ask, 4),
            "bid_size": round(bid_size, 2),
            "ask_size": round(ask_size, 2),
            "no_bid": round(no_bid, 4),
            "no_ask": round(no_ask, 4),
            "no_bid_size": round(no_bid_size, 2),
            "no_ask_size": round(no_ask_size, 2),
            "no_live": ob_no is not None,
            "live": ob is not None,
        }

    # ---- edges -----------------------------------------------------------
    @staticmethod
    def _ask(q: Optional[dict]) -> Optional[float]:
        if not q or not q.get("ask"):
            return None
        return q["ask"]

    @staticmethod
    def _bid(q: Optional[dict]) -> Optional[float]:
        if not q or not q.get("bid"):
            return None
        return q["bid"]

    def _onex2_edges(self, q_home, q_draw, q_away) -> List[dict]:
        edges: List[dict] = []
        asks = [self._ask(q_home), self._ask(q_draw), self._ask(q_away)]
        bids = [self._bid(q_home), self._bid(q_draw), self._bid(q_away)]
        if all(a is not None for a in asks):
            cost = sum(asks)
            if cost < 1.0:
                size = min(q["ask_size"] for q in (q_home, q_draw, q_away))
                edges.append(
                    {
                        "type": "1x2_back_arb",
                        "edge": round(1.0 - cost, 4),
                        "detail": f"买入 主+平+客 YES，成本 {cost:.3f} < 1",
                        "size": round(size, 2),
                    }
                )
        if all(b is not None for b in bids):
            credit = sum(bids)
            if credit > 1.0:
                size = min(q["bid_size"] for q in (q_home, q_draw, q_away))
                edges.append(
                    {
                        "type": "1x2_lay_arb",
                        "edge": round(credit - 1.0, 4),
                        "detail": f"卖出 主+平+客 YES，收入 {credit:.3f} > 1",
                        "size": round(size, 2),
                    }
                )
        return edges

    def _score_edges(self, score_quotes: List[dict]) -> List[dict]:
        edges: List[dict] = []
        if len(score_quotes) < 2:
            return edges
        asks = [self._ask(q) for q in score_quotes]
        bids = [self._bid(q) for q in score_quotes]
        if all(a is not None for a in asks):
            cost = sum(asks)
            if cost < 1.0:
                size = min(q["ask_size"] for q in score_quotes)
                edges.append(
                    {
                        "type": "score_back_arb",
                        "edge": round(1.0 - cost, 4),
                        "detail": f"买入全部 {len(asks)} 个比分 YES，成本 {cost:.3f} < 1",
                        "size": round(size, 2),
                    }
                )
        if all(b is not None for b in bids):
            credit = sum(bids)
            if credit > 1.0:
                size = min(q["bid_size"] for q in score_quotes)
                edges.append(
                    {
                        "type": "score_lay_arb",
                        "edge": round(credit - 1.0, 4),
                        "detail": f"卖出全部 {len(bids)} 个比分 YES，收入 {credit:.3f} > 1",
                        "size": round(size, 2),
                    }
                )
        return edges

    @staticmethod
    def _overround(quotes: List[Optional[dict]]) -> Optional[float]:
        asks = [q["ask"] for q in quotes if q and q.get("ask")]
        if not asks or len(asks) != len([q for q in quotes if q]):
            return None
        return round(sum(asks), 4)

    # ---- serialization ---------------------------------------------------
    def _game_dict(self, game: Game, now: datetime) -> dict:
        q_home = self._quote(game.home_win)
        q_draw = self._quote(game.draw)
        q_away = self._quote(game.away_win)
        score_quotes = [self._quote(o) for o in game.scores]
        score_quotes = [q for q in score_quotes if q]

        edges = self._onex2_edges(q_home, q_draw, q_away) + self._score_edges(score_quotes)
        score_model_out = self._score_model(q_home, q_draw, q_away, score_quotes)

        return {
            "slug": game.slug,
            "home": game.home,
            "away": game.away,
            "kickoff": game.kickoff,
            "status": game.status(now),
            "onex2": {
                "home": q_home,
                "draw": q_draw,
                "away": q_away,
                "overround": self._overround([q_home, q_draw, q_away]),
            },
            "scores": score_quotes,
            "scores_overround": self._overround(score_quotes) if score_quotes else None,
            "edges": edges,
            "score_model": score_model_out,
        }

    @staticmethod
    def _mid(q: Optional[dict]) -> float:
        if not q:
            return 0.0
        bid, ask = q.get("bid") or 0.0, q.get("ask") or 0.0
        if bid and ask:
            return (bid + ask) / 2
        return bid or ask or 0.0

    def _score_model(self, q_home, q_draw, q_away, score_quotes) -> Optional[dict]:
        """Model-derived value matrix (SCORE_MATRIX.md). Returns None if inputs thin."""
        if not score_quotes or len(score_quotes) < 3:
            return None
        mids = (self._mid(q_home), self._mid(q_draw), self._mid(q_away))
        if min(mids) <= 0:
            return None
        try:
            return score_model.build_score_model(
                mids,
                score_quotes,
                model=config.SCORE_MODEL,
                rho=config.SCORE_RHO,
                devig_method=config.DEVIG_METHOD,
                threshold=config.VALUE_EDGE_THRESHOLD,
            )
        except Exception:  # noqa: BLE001 — never let the model break the snapshot
            return None

    def snapshot(self) -> dict:
        now = datetime.now(timezone.utc)
        games = [self._game_dict(g, now) for g in self.games]
        total_edges = sum(len(g["edges"]) for g in games)
        return {
            "type": "snapshot",
            "updated_at": int(time.time()),
            "ws_connected": self.ws.is_connected,
            "subscribed_tokens": self.ws.subscribed_count,
            "edge_count": total_edges,
            "games": games,
        }

    def snapshot_meta_time(self) -> int:
        return int(time.time())

    def game_dict(self, slug: str) -> Optional[dict]:
        now = datetime.now(timezone.utc)
        for g in self.games:
            if g.slug == slug:
                return self._game_dict(g, now)
        return None

    def token_to_slug(self) -> Dict[str, str]:
        index: Dict[str, str] = {}
        for g in self.games:
            for t in g.all_tokens():
                index[t] = g.slug
        return index

    # ---- helpers for the paper trader -----------------------------------
    def find_game(self, slug: str) -> Optional[Game]:
        for g in self.games:
            if g.slug == slug:
                return g
        return None

    def find_outcome(self, game: Game, market: str, label: str) -> Optional[Outcome]:
        if market == "1x2":
            return {"home": game.home_win, "draw": game.draw, "away": game.away_win}.get(label)
        for o in game.scores:
            if o.label == label:
                return o
        return None

    def token_best(self, token_id: str) -> dict:
        """Best bid/ask (price + size) for a token; zeros if no book."""
        ob = self.ws.get_orderbook(token_id)
        if ob is None:
            return {"bid": 0.0, "bid_size": 0.0, "ask": 0.0, "ask_size": 0.0, "live": False}
        bid, bid_size = ob.best_bid_level()
        ask, ask_size = ob.best_ask_level()
        return {"bid": bid, "bid_size": bid_size, "ask": ask, "ask_size": ask_size, "live": True}

    def fair_for(self, slug: str, market: str, label: str, side: str) -> Optional[float]:
        """Current model fair value of the held token (side 'yes'/'no')."""
        game = self.find_game(slug)
        if not game:
            return None
        if market == "score":
            sm = self._score_model(
                self._quote(game.home_win),
                self._quote(game.draw),
                self._quote(game.away_win),
                [q for q in (self._quote(o) for o in game.scores) if q],
            )
            if not sm:
                return None
            cell = sm["matrix"].get(label)
            if not cell:
                return None
            p = cell["model"]
        else:  # 1x2 — de-vig current mids
            mids = [self._mid(self._quote(x)) for x in (game.home_win, game.draw, game.away_win)]
            if min(mids) <= 0:
                return None
            probs = score_model.devig(mids, config.DEVIG_METHOD)
            idx = {"home": 0, "draw": 1, "away": 2}.get(label)
            if idx is None:
                return None
            p = probs[idx]
        return p if side == "yes" else 1.0 - p
