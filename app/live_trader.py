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
        # runtime-editable trade sizing (seeded from env defaults)
        self.max_per_leg = config.LIVE_MAX_PER_LEG
        self.max_total = config.LIVE_MAX_TOTAL
        self.min_edge = config.LIVE_MIN_EDGE
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

    def set_config(self, updates: dict) -> dict:
        """Runtime-adjust trade sizing. Returns the applied caps."""
        applied = {}
        if "max_per_leg" in updates:
            try:
                self.max_per_leg = max(1.0, min(10000.0, float(updates["max_per_leg"])))
                applied["max_per_leg"] = self.max_per_leg
            except (TypeError, ValueError):
                pass
        if "max_total" in updates:
            try:
                self.max_total = max(1.0, min(1_000_000.0, float(updates["max_total"])))
                applied["max_total"] = self.max_total
            except (TypeError, ValueError):
                pass
        if "min_edge" in updates:
            try:
                self.min_edge = max(0.0, min(0.5, float(updates["min_edge"])))
                applied["min_edge"] = self.min_edge
            except (TypeError, ValueError):
                pass
        if applied:
            self._note("参数更新: " + ", ".join(f"{k}={v}" for k, v in applied.items()))
        return {"ok": True, "caps": {"per_leg": self.max_per_leg, "total": self.max_total, "min_edge": self.min_edge}}

    # ---- manual test buy (real money, small) ----------------------------
    def test_buy(self, slug: Optional[str] = None) -> dict:
        """Place a REAL small 3-leg back buy (YES x3) on a chosen/any game to
        validate the order path end-to-end. Not an arb — a 1X2 back held to
        settlement returns ~$1/set, so the net cost is just the overround."""
        if not self.enabled:
            return {"ok": False, "error": "LIVE_ENABLED=0 (主闸关闭)"}
        if not self.ready:
            return {"ok": False, "error": self.error or "未就绪 (先测试连接)"}
        game, quotes = None, None
        for g in self.state.games:
            if slug and g.slug != slug:
                continue
            if not (g.home_win and g.draw and g.away_win) or g.status() == "live":
                continue
            q = [self.state.token_best(o.yes_token) for o in (g.home_win, g.draw, g.away_win)]
            if all(x["ask"] > 0 and x["ask_size"] > 0 for x in q):
                game, quotes = g, q
                break
        if not game:
            return {"ok": False, "error": "找不到可测试的市场(需三腿都有卖单+深度)"}
        import math
        legs = [game.home_win, game.draw, game.away_win]
        asks = [x["ask"] for x in quotes]
        sizes = [x["ask_size"] for x in quotes]
        pusd = (self.onchain or {}).get("pusd") or 0.0
        # Per-leg budget; the CLOB enforces a $1 minimum notional per order, so
        # size each leg to the cheapest integer share count that clears $1.
        budget = min(self.max_per_leg, pusd / 3 if pusd else self.max_per_leg)
        MIN_NOTIONAL = 1.0
        n_legs = []
        for ask, depth in zip(asks, sizes):
            need = max(1, math.ceil(MIN_NOTIONAL / ask))      # shares to reach $1
            cap = int(min(budget / ask, depth))               # affordable + in book
            n_legs.append(float(need) if need <= cap else 0.0)
        if any(n < 1 for n in n_legs):
            return {"ok": False, "error": f"测试额度/深度不足以满足每腿$1最小单 "
                    f"(每腿预算≈${budget:.2f}, 需≥${MIN_NOTIONAL:.0f}/腿, pUSD ${pusd:.2f})"}
        neg = any(getattr(o, "neg_risk", False) for o in legs)
        tokens = [o.yes_token for o in legs]
        labels = ["home", "draw", "away"]
        self._note(f"🧪 测试买入 {game.home} vs {game.away} 三腿YES N={'/'.join(f'{n:.0f}' for n in n_legs)} "
                   f"(非套利,验证下单链路)", "warn")
        import concurrent.futures
        res = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futs = {ex.submit(self._place_fok, tokens[i], asks[i], n_legs[i], neg): i for i in range(3)}
            for fut in concurrent.futures.as_completed(futs):
                res[futs[fut]] = fut.result()
        filled = [i for i in range(3) if res[i].get("filled")]
        cost = sum(n_legs[i] * asks[i] for i in filled)
        self.deployed += cost
        self.baskets.append({"ts": int(time.time()), "slug": game.slug, "home": game.home,
                             "away": game.away, "kind": "test", "shares": "/".join(f"{n:.0f}" for n in n_legs),
                             "legs": [{"leg": labels[i], "price": round(asks[i], 4), "n": n_legs[i], **res[i]} for i in range(3)],
                             "filled_legs": len(filled), "cost": round(cost, 2),
                             "complete": len(filled) == 3, "done": True})
        self._note(f"🧪 测试结果: {len(filled)}/3 成交, 成本≈${cost:.2f}", "info")
        self.refresh_balance()
        return {"ok": True, "game": f"{game.home} vs {game.away}",
                "shares": "/".join(f"{n:.0f}" for n in n_legs),
                "filled": len(filled), "cost": round(cost, 2),
                "legs": [{"leg": labels[i], **res[i]} for i in range(3)]}

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
                sizes = [x["ask_size"] for x in q]
                if self._valid_arb(asks, sizes, 1.0):
                    opened += self._execute(g, "back", legs, "yes", asks, sizes)
            if (g.slug, "lay") not in held:
                q = [self.state.token_best(o.no_token) for o in legs]
                asks = [x["ask"] for x in q]
                sizes = [x["ask_size"] for x in q]
                if self._valid_arb(asks, sizes, 2.0):
                    opened += self._execute(g, "lay", legs, "no", asks, sizes)
        return {"opened": opened}

    def _valid_arb(self, asks, sizes, payout) -> bool:
        """Real arb gate: all legs priced AND have real depth, profit/set in (min,max].
        payout=1 (back, buy YES) or 2 (lay, buy NO). Rejects stale/thin artifacts."""
        if not all(a and a > 0 for a in asks):
            return False
        if not all(s and s > 0 for s in sizes):   # every leg needs real top-of-book depth
            return False
        edge = payout - sum(asks)                  # profit per set ($)
        return self.min_edge <= edge <= config.LIVE_MAX_EDGE

    def _execute(self, game, kind, legs, side, prices, sizes) -> int:
        if self.deployed >= self.max_total:
            return 0
        # available capital = min(total cap remaining, actual pUSD balance)
        pusd = (self.onchain or {}).get("pusd") or 0.0
        available = min(self.max_total - self.deployed, pusd)
        per_leg = min(self.max_per_leg, available / 3)
        if per_leg < 1:
            self._note(f"{game.home} [{kind}] 跳过: 可用资金不足(pUSD≈${pusd:.2f})", "warn")
            return 0
        n = float(int(min([per_leg / max(prices)] + [s for s in sizes if s > 0])))
        if n < 1:
            return 0
        tokens = [o.yes_token if side == "yes" else o.no_token for o in legs]
        neg = any(getattr(o, "neg_risk", False) for o in legs)
        labels = ["home", "draw", "away"]
        # ① probe the THINNEST leg first (riskiest to fill) with zero prior exposure
        order_idx = sorted(range(3), key=lambda i: sizes[i] if sizes[i] > 0 else 1e18)
        probe = order_idx[0]
        self._note(f"⚔️ {game.home} vs {game.away} [{kind}] N={n:.0f}股 · 探路腿={labels[probe]}(量{sizes[probe]:.0f})", "warn")
        res = {probe: self._place_fok(tokens[probe], prices[probe], n, neg)}
        if not res[probe].get("filled"):
            self._note(f"探路腿 {labels[probe]} 未成交 → 放弃（无敞口）", "info")
            self.baskets.append({"ts": int(time.time()), "slug": game.slug, "home": game.home,
                                 "away": game.away, "kind": kind, "shares": n,
                                 "legs": [{"leg": labels[probe], "price": round(prices[probe], 4), **res[probe]}],
                                 "filled_legs": 0, "cost": 0.0, "complete": False,
                                 "note": "探路未成,无敞口", "done": True})
            return 1
        # ② probe filled -> fire the other two legs CONCURRENTLY (shrink the window)
        import concurrent.futures
        others = order_idx[1:]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futs = {ex.submit(self._place_fok, tokens[i], prices[i], n, neg): i for i in others}
            for fut in concurrent.futures.as_completed(futs):
                res[futs[fut]] = fut.result()
        filled = [i for i in range(3) if res[i].get("filled")]
        cost = sum(n * prices[i] for i in filled)
        ok = len(filled) == 3
        # ③ partial fill -> auto-unwind the filled legs to flatten the directional exposure
        unwound = []
        if not ok:
            self._note(f"⚠️ 仅 {len(filled)}/3 成交 → 自动平腿 {[labels[i] for i in filled]}", "warn")
            for i in filled:
                u = self._unwind(tokens[i], n, neg)
                unwound.append({"leg": labels[i], **u})
        self.deployed += cost
        self.baskets.append({"ts": int(time.time()), "slug": game.slug, "home": game.home,
                             "away": game.away, "kind": kind, "shares": n,
                             "legs": [{"leg": labels[i], "price": round(prices[i], 4), **res[i]} for i in range(3)],
                             "filled_legs": len(filled), "cost": round(cost, 2), "complete": ok,
                             "unwound": unwound, "done": True})
        self._note((f"✅ {game.home} [{kind}] 三腿全成交 ≈${cost:.2f}" if ok
                    else f"⚠️ {game.home} [{kind}] {len(filled)}/3成交,已尝试平腿"),
                   "info" if ok else "warn")
        self.refresh_balance()
        return 1

    def _unwind(self, token_id, size, neg_risk) -> dict:
        """Flatten a filled leg by market-SELL (FOK) at the current bid."""
        b = self.state.token_best(token_id)
        bid = b.get("bid", 0)
        if not (bid and bid > 0):
            self._note(f"平腿失败: {token_id[:10]} 无买价 → 敞口留存!", "warn")
            return {"unwound": False, "reason": "no bid"}
        r = self._place_sell(token_id, bid, size, neg_risk)
        self._note(f"平腿 {token_id[:10]} 卖@{bid:.3f}: {r.get('status')}", "warn")
        return {"unwound": r.get("filled", False), **r}

    def _place_sell(self, token_id, price, size, neg_risk) -> dict:
        try:
            from .poly.signer import Order, ZERO_BYTES32_HEX
            px = max(0.001, round(float(price) - 0.001, 3))  # cross down to ensure fill
            order = Order(token_id=str(token_id), price=px, size=float(size), side="SELL",
                          maker=self.funder, signature_type=config.POLY_SIGNATURE_TYPE,
                          neg_risk=neg_risk, builder_code=config.POLY_BUILDER_CODE or ZERO_BYTES32_HEX)
            # vendored Order uses the BUY amount formula; swap for SELL (maker gives shares)
            order.maker_amount = str(int(size * 1_000_000))
            order.taker_amount = str(int(size * px * 1_000_000))
            signed = self.signer.sign_order(order)
            resp = self.client.post_order(signed, order_type="FOK")
            resp = resp if isinstance(resp, dict) else {}
            return {"filled": resp.get("status") == "matched", "status": resp.get("status"),
                    "orderID": resp.get("orderID"), "errorMsg": resp.get("errorMsg")}
        except Exception as exc:  # noqa: BLE001
            self._note(f"平腿下单异常: {type(exc).__name__}: {exc}", "warn")
            return {"filled": False, "status": "error", "error": str(exc)[:200]}

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
            "caps": {"per_leg": self.max_per_leg, "total": self.max_total,
                     "min_edge": self.min_edge},
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
