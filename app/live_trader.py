"""
LIVE 1X2 arbitrage executor — REAL MONEY via vendored CLOB **V2** signer/client
(app/poly: 0xE111 exchange, domain version "2", pUSD collateral, builder field).

Safety gates (ALL must hold to send a real order):
  1. config.LIVE_ENABLED (env LIVE_ENABLED=1)
  2. private key configured (env POLY_PRIVATE_KEY) and client initialised OK
  3. runtime self.armed == True (set via /api/live/arm; default False)
  4. caps: <= LIVE_MAX_PER_LEG/leg, <= LIVE_MAX_TOTAL cumulative, edge >= LIVE_MIN_EDGE

Strategy: back (buy YES x3, sum<1) / lay (buy NO x3, sum<2), EQUAL SHARES via
LIMIT FOK. Three legs are NOT atomic — partial fills leave a directional position
(bounded by the tiny per-leg cap, flagged in the UI). The private key is read
from env, used only to construct the signer, and NEVER logged/returned/sent out.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Deque, List, Optional

from . import config

logger = logging.getLogger(__name__)


class LiveTrader:
    def __init__(self, state) -> None:
        self.state = state
        self.enabled = config.LIVE_ENABLED
        self.armed = False
        self.signer = None
        self.client = None
        self.ready = False
        self.error: Optional[str] = None
        self.address: Optional[str] = None
        self.funder: Optional[str] = config.POLY_FUNDER or None
        self.onchain: dict = {}
        self.deployed = 0.0
        self.baskets: List[dict] = []
        self.log: Deque[dict] = deque(maxlen=80)

    def _note(self, desc: str, level: str = "info") -> None:
        self.log.appendleft({"ts": int(time.time()), "desc": desc, "level": level})
        getattr(logger, "warning" if level == "warn" else "info")("LIVE: %s", desc)

    # ---- init -----------------------------------------------------------
    def init(self) -> None:
        if not self.enabled:
            self.error = "LIVE_ENABLED=0 (主闸关闭)"
            return
        if not config.POLY_PRIVATE_KEY:
            self.error = "未配置 POLY_PRIVATE_KEY"
            return
        if not self.funder:
            self.error = "未配置 POLY_FUNDER"
            return
        try:
            from .poly.signer import OrderSigner
            from .poly.client import ClobClient
            self.signer = OrderSigner(config.POLY_PRIVATE_KEY, chain_id=137)
            self.address = self.signer.address
            self.client = ClobClient(host=config.CLOB_HOST, chain_id=137,
                                     signature_type=config.POLY_SIGNATURE_TYPE, funder=self.funder)
            creds = self.client.create_or_derive_api_key(self.signer)
            if not creds.is_valid():
                raise RuntimeError("API 凭证派生失败")
            self.client.api_creds = creds
            self.ready = True
            self.error = None
            self._note(f"已连接 CLOB V2 · 签名 {self.address} · funder {self.funder} · "
                       f"builder={'有' if config.POLY_BUILDER_CODE else '无'}")
            self.refresh_balance()
        except Exception as exc:  # noqa: BLE001
            self.ready = False
            self.error = f"初始化失败: {type(exc).__name__}: {exc}"
            self._note(self.error, "warn")

    def refresh_balance(self) -> None:
        try:
            from .poly.onchain import check_funder
            self.onchain = check_funder(self.funder)
            if self.onchain.get("error"):
                self._note(f"链上查询: {self.onchain['error']}", "warn")
        except Exception as exc:  # noqa: BLE001
            self._note(f"查余额失败: {exc}", "warn")

    # ---- control --------------------------------------------------------
    def arm(self, on: bool) -> dict:
        if on and not self.ready:
            return {"ok": False, "error": self.error or "未就绪，无法武装"}
        if on and not (self.onchain.get("pusd") and self.onchain["pusd"] > 0):
            return {"ok": False, "error": "funder 无 pUSD 余额，武装无意义（先充 pUSD）"}
        self.armed = bool(on)
        self._note("⚔️ 已武装(真实下单已启用)" if self.armed else "已解除武装", "warn")
        return {"ok": True, "armed": self.armed}

    # ---- execution ------------------------------------------------------
    def scan_once(self) -> dict:
        if not (self.enabled and self.ready and self.armed):
            return {"opened": 0, "reason": "未启用/未就绪/未武装"}
        held = {(b["slug"], b["kind"]) for b in self.baskets if not b.get("done")}
        opened = 0
        for g in self.state.games:
            legs = [g.home_win, g.draw, g.away_win]
            if not all(legs) or g.status() == "live":
                continue
            if (g.slug, "back") not in held:
                q = [self.state.token_best(o.yes_token) for o in legs]
                asks = [x["ask"] for x in q]
                if all(a > 0 for a in asks) and sum(asks) < 1 - config.LIVE_MIN_EDGE:
                    opened += self._execute(g, "back", legs, "yes", asks, [x["ask_size"] for x in q])
            if (g.slug, "lay") not in held:
                q = [self.state.token_best(o.no_token) for o in legs]
                asks = [x["ask"] for x in q]
                if all(a > 0 for a in asks) and sum(asks) < 2 - config.LIVE_MIN_EDGE:
                    opened += self._execute(g, "lay", legs, "no", asks, [x["ask_size"] for x in q])
        return {"opened": opened}

    def _execute(self, game, kind, legs, side, prices, sizes) -> int:
        if self.deployed >= config.LIVE_MAX_TOTAL:
            return 0
        per_leg = min(config.LIVE_MAX_PER_LEG, (config.LIVE_MAX_TOTAL - self.deployed) / 3)
        if per_leg < 1:
            return 0
        n = float(int(min([per_leg / max(prices)] + [s for s in sizes if s > 0])))
        if n < 1:
            return 0
        tokens = [o.yes_token if side == "yes" else o.no_token for o in legs]
        neg = any(getattr(o, "neg_risk", False) for o in legs)
        labels = ["home", "draw", "away"]
        self._note(f"⚔️ 执行 {game.home} vs {game.away} [{kind}] N={n:.0f}股×3 (neg={neg})", "warn")
        results, filled, cost = [], 0, 0.0
        for lbl, tok, px in zip(labels, tokens, prices):
            r = self._place_fok(tok, px, n, neg)
            results.append({"leg": lbl, "price": round(px, 4), **r})
            if r.get("filled"):
                filled += 1
                cost += n * px
        self.deployed += cost
        ok = filled == 3
        self.baskets.append({"ts": int(time.time()), "slug": game.slug, "home": game.home,
                             "away": game.away, "kind": kind, "shares": n, "legs": results,
                             "filled_legs": filled, "cost": round(cost, 2), "complete": ok, "done": True})
        self._note((f"✅ {game.home} [{kind}] 三腿全成交 ≈${cost:.2f}" if ok
                    else f"⚠️ {game.home} [{kind}] 仅 {filled}/3 成交 → 单边敞口! ≈${cost:.2f}"),
                   "info" if ok else "warn")
        self.refresh_balance()
        return 1

    def _place_fok(self, token_id, price, size, neg_risk) -> dict:
        try:
            from .poly.signer import Order, ZERO_BYTES32_HEX
            from .poly.client import OrderError
            px = min(0.999, round(float(price), 3))
            order = Order(token_id=str(token_id), price=px, size=float(size), side="BUY",
                          maker=self.funder, signature_type=config.POLY_SIGNATURE_TYPE,
                          neg_risk=neg_risk,
                          builder_code=config.POLY_BUILDER_CODE or ZERO_BYTES32_HEX)
            signed = self.signer.sign_order(order)
            resp = self.client.post_order(signed, order_type="FOK")
            resp = resp if isinstance(resp, dict) else {}
            status = resp.get("status")          # "matched" => fully filled (FOK)
            return {"filled": status == "matched", "status": status,
                    "orderID": resp.get("orderID"), "errorMsg": resp.get("errorMsg")}
        except Exception as exc:  # noqa: BLE001
            self._note(f"下单异常: {type(exc).__name__}: {exc}", "warn")
            return {"filled": False, "status": "error", "error": str(exc)[:200]}

    # ---- status (NEVER returns the key) ---------------------------------
    def status(self) -> dict:
        oc = self.onchain or {}
        return {
            "enabled": self.enabled, "ready": self.ready, "armed": self.armed, "error": self.error,
            "address": self.address, "funder": self.funder,
            "pusd": oc.get("pusd"),
            "allow_exchange": oc.get("allow_exchange"), "allow_negrisk": oc.get("allow_negrisk"),
            "onchain_error": oc.get("error"),
            "deployed": round(self.deployed, 2),
            "caps": {"per_leg": config.LIVE_MAX_PER_LEG, "total": config.LIVE_MAX_TOTAL,
                     "min_edge": config.LIVE_MIN_EDGE},
            "builder": bool(config.POLY_BUILDER_CODE),
            "baskets": list(reversed(self.baskets[-40:])),
            "log": list(self.log),
        }

    async def run(self) -> None:
        import asyncio
        if self.enabled and config.POLY_PRIVATE_KEY and not self.ready:
            self.init()
        while True:
            await asyncio.sleep(config.LIVE_INTERVAL)
            if not (self.enabled and self.ready and self.armed):
                continue
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001
                self._note(f"扫描异常: {exc}", "warn")
