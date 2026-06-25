"""
LIVE 1X2 arbitrage executor — REAL MONEY via py-clob-client.

Safety gates (ALL must hold for a real order to be sent):
  1. config.LIVE_ENABLED (env LIVE_ENABLED=1)
  2. a private key is configured (env POLY_PRIVATE_KEY) and the client initialised OK
  3. runtime self.armed == True (set via /api/live/arm from the UI; default False)
  4. caps: <= LIVE_MAX_PER_LEG per leg, <= LIVE_MAX_TOTAL cumulative, edge >= LIVE_MIN_EDGE

Strategy mirrors the paper arb (back: buy YES x3 sum<1; lay: buy NO x3 sum<2),
EQUAL SHARES per leg via LIMIT FOK orders (each leg fully fills or cancels).
Three legs are NOT atomic — a partial fill (some legs cancel) leaves a directional
position; with the tiny per-leg cap the damage is bounded, and partials are flagged.

The private key is read from env, used only to construct the signer, and NEVER
logged, returned by the API, or sent to the browser.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Deque, List, Optional

from . import config

logger = logging.getLogger(__name__)

BUY = "BUY"


class LiveTrader:
    def __init__(self, state) -> None:
        self.state = state
        self.enabled = config.LIVE_ENABLED
        self.armed = False                # runtime gate — must be turned on explicitly
        self.client = None
        self.creds = None
        self.ready = False
        self.error: Optional[str] = None
        self.address: Optional[str] = None
        self.funder: Optional[str] = config.POLY_FUNDER or None
        self.usdc: Optional[float] = None
        self.deployed = 0.0               # cumulative USDC sent live
        self.baskets: List[dict] = []
        self.log: Deque[dict] = deque(maxlen=80)
        self._last_balance_ts = 0

    # ---- init / connection ----------------------------------------------
    def _note(self, desc: str, level: str = "info") -> None:
        self.log.appendleft({"ts": int(time.time()), "desc": desc, "level": level})
        getattr(logger, "warning" if level == "warn" else "info")("LIVE: %s", desc)

    def init(self) -> None:
        """Construct the CLOB client + derive API creds. Never logs the key."""
        if not self.enabled:
            self.error = "LIVE_ENABLED=0 (master gate off)"
            return
        if not config.POLY_PRIVATE_KEY:
            self.error = "未配置 POLY_PRIVATE_KEY"
            return
        try:
            from py_clob_client.client import ClobClient
            from py_builder_signing_sdk.config import BuilderConfig
            from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

            builder = None
            if config.POLY_BUILDER_API_KEY:
                builder = BuilderConfig(local_builder_creds=BuilderApiKeyCreds(
                    api_key=config.POLY_BUILDER_API_KEY,
                    secret=config.POLY_BUILDER_SECRET,
                    passphrase=config.POLY_BUILDER_PASSPHRASE))
            self.client = ClobClient(
                config.CLOB_HOST, chain_id=137, key=config.POLY_PRIVATE_KEY,
                signature_type=config.POLY_SIGNATURE_TYPE,
                funder=self.funder, builder_config=builder)
            self.address = self.client.get_address()
            self.creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(self.creds)
            self.ready = True
            self.error = None
            self._note(f"已连接 CLOB · 签名地址 {self.address} · funder {self.funder} · builder={'有' if builder else '无'}")
            self.refresh_balance()
        except Exception as exc:  # noqa: BLE001
            self.ready = False
            self.error = f"初始化失败: {type(exc).__name__}: {exc}"
            self._note(self.error, "warn")

    def refresh_balance(self) -> None:
        if not self.ready:
            return
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            r = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            bal = r.get("balance") if isinstance(r, dict) else None
            if bal is not None:
                self.usdc = round(float(bal) / 1e6, 2)  # USDC has 6 decimals
            self._last_balance_ts = int(time.time())
        except Exception as exc:  # noqa: BLE001
            self._note(f"查余额失败: {exc}", "warn")

    # ---- control --------------------------------------------------------
    def arm(self, on: bool) -> dict:
        if on and not self.ready:
            return {"ok": False, "error": self.error or "未就绪，无法武装"}
        self.armed = bool(on)
        self._note(f"{'⚔️ 已武装(真实下单已启用)' if self.armed else '已解除武装'}", "warn")
        return {"ok": True, "armed": self.armed}

    # ---- execution ------------------------------------------------------
    def scan_once(self) -> dict:
        if not (self.enabled and self.ready and self.armed):
            return {"opened": 0, "reason": "未启用/未就绪/未武装"}
        held = {(b["slug"], b["kind"]) for b in self.baskets if not b.get("done")}
        opened = 0
        for g in self.state.games:
            legs = [g.home_win, g.draw, g.away_win]
            if not all(legs):
                continue
            if g.status() == "live":   # only pre-match to keep model/price stable
                continue
            # back: buy YES x3, sum(ask_yes) < 1
            if (g.slug, "back") not in held:
                q = [self.state.token_best(o.yes_token) for o in legs]
                asks = [x["ask"] for x in q]
                if all(a > 0 for a in asks) and sum(asks) < 1 - config.LIVE_MIN_EDGE:
                    opened += self._execute(g, "back", legs, "yes", asks, [x["ask_size"] for x in q])
            # lay: buy NO x3, sum(ask_no) < 2
            if (g.slug, "lay") not in held:
                q = [self.state.token_best(o.no_token) for o in legs]
                asks = [x["ask"] for x in q]
                if all(a > 0 for a in asks) and sum(asks) < 2 - config.LIVE_MIN_EDGE:
                    opened += self._execute(g, "lay", legs, "no", asks, [x["ask_size"] for x in q])
        return {"opened": opened}

    def _execute(self, game, kind, legs, side, prices, sizes) -> int:
        # remaining global budget
        if self.deployed >= config.LIVE_MAX_TOTAL:
            return 0
        per_leg = min(config.LIVE_MAX_PER_LEG, (config.LIVE_MAX_TOTAL - self.deployed) / 3)
        if per_leg < 1:
            return 0
        # equal shares N so each leg costs <= per_leg; bounded by book depth
        max_price = max(prices)
        n = per_leg / max_price
        n = min([n] + [s for s in sizes if s > 0])
        n = float(int(n))  # whole shares
        if n < 1:
            self._note(f"{game.home} vs {game.away} [{kind}] 跳过: 可成交份额<1", "warn")
            return 0
        tokens = [o.yes_token if side == "yes" else o.no_token for o in legs]
        neg = any(getattr(o, "neg_risk", False) for o in legs)
        labels = ["home", "draw", "away"]
        results, filled = [], 0
        cost = 0.0
        self._note(f"⚔️ 执行 {game.home} vs {game.away} [{kind}] N={n:.0f}股 ×3腿 (neg_risk={neg})", "warn")
        for lbl, tok, px in zip(labels, tokens, prices):
            r = self._place_fok(tok, px, n, neg)
            results.append({"leg": lbl, "token": tok, "price": px, **r})
            if r.get("filled"):
                filled += 1
                cost += n * px
        self.deployed += cost
        ok = filled == 3
        self.baskets.append({
            "ts": int(time.time()), "slug": game.slug, "home": game.home, "away": game.away,
            "kind": kind, "shares": n, "legs": results, "filled_legs": filled,
            "cost": round(cost, 2), "complete": ok, "done": True,
        })
        if ok:
            self._note(f"✅ {game.home} [{kind}] 三腿全成交, 成本≈${cost:.2f}", "info")
        else:
            self._note(f"⚠️ {game.home} [{kind}] 仅 {filled}/3 腿成交 → 存在单边敞口! 成本≈${cost:.2f}", "warn")
        self.refresh_balance()
        return 1

    def _place_fok(self, token_id, price, size, neg_risk) -> dict:
        """Place a LIMIT FOK BUY order: fully fills `size` shares at <= price, or cancels."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
            # cap price at 0.99, round to a safe tick (0.001) and pad slightly to ensure FOK fill
            px = min(0.999, round(price + 0.001, 3))
            args = OrderArgs(token_id=str(token_id), price=px, size=float(size), side=BUY)
            signed = self.client.create_order(args, PartialCreateOrderOptions(neg_risk=neg_risk))
            resp = self.client.post_order(signed, OrderType.FOK)
            resp = resp if isinstance(resp, dict) else {}
            status = resp.get("status")          # FOK -> "matched" means fully filled
            filled = status == "matched"
            return {"filled": filled, "status": status, "orderID": resp.get("orderID"),
                    "errorMsg": resp.get("errorMsg")}
        except Exception as exc:  # noqa: BLE001
            self._note(f"下单异常: {type(exc).__name__}: {exc}", "warn")
            return {"filled": False, "status": "error", "error": str(exc)[:200]}

    # ---- status (NEVER returns the private key) -------------------------
    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "armed": self.armed,
            "error": self.error,
            "address": self.address,
            "funder": self.funder,
            "usdc": self.usdc,
            "deployed": round(self.deployed, 2),
            "caps": {"per_leg": config.LIVE_MAX_PER_LEG, "total": config.LIVE_MAX_TOTAL,
                     "min_edge": config.LIVE_MIN_EDGE},
            "baskets": list(reversed(self.baskets[-40:])),
            "log": list(self.log),
        }

    async def run(self) -> None:
        import asyncio
        # one-time init on startup if enabled
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
