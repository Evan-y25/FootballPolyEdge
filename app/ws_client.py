"""
Polymarket CLOB market websocket client.

Adapted/trimmed from ReferenceProject's `src/websocket_client.py`:
- maintains a live orderbook cache keyed by CLOB token id
- handles `book` (full snapshot) and `price_change` (incremental) events
- auto-reconnects and chunks large subscriptions
- fires `on_update(token_id)` whenever a book changes
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Dict, List, Optional, Set

import websockets

from . import config

logger = logging.getLogger(__name__)


class Orderbook:
    """Live orderbook for one token: price -> size maps for bids and asks."""

    __slots__ = ("bids", "asks", "ts")

    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.ts: int = 0

    def apply_snapshot(self, msg: dict) -> None:
        self.bids = {
            float(b["price"]): float(b["size"])
            for b in msg.get("bids", [])
            if float(b.get("size", 0)) > 0
        }
        self.asks = {
            float(a["price"]): float(a["size"])
            for a in msg.get("asks", [])
            if float(a.get("size", 0)) > 0
        }
        self.ts = int(msg.get("timestamp", 0) or 0)

    def apply_change(self, price: float, size: float, side: str) -> None:
        book = self.bids if side.upper() in ("BUY", "BID") else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size

    @property
    def best_bid(self) -> float:
        return max(self.bids) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return min(self.asks) if self.asks else 0.0

    def best_bid_level(self) -> tuple:
        if not self.bids:
            return 0.0, 0.0
        p = max(self.bids)
        return p, self.bids[p]

    def best_ask_level(self) -> tuple:
        if not self.asks:
            return 0.0, 0.0
        p = min(self.asks)
        return p, self.asks[p]


UpdateCallback = Callable[[str], None]


class MarketWebSocket:
    """Single websocket connection to the CLOB market channel."""

    def __init__(self, url: str = config.WSS_MARKET_URL) -> None:
        self.url = url
        self.orderbooks: Dict[str, Orderbook] = {}
        self._assets: Set[str] = set()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._connected = False
        self._on_update: Optional[UpdateCallback] = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def subscribed_count(self) -> int:
        return len(self._assets)

    def on_update(self, callback: UpdateCallback) -> UpdateCallback:
        self._on_update = callback
        return callback

    def get_orderbook(self, token_id: str) -> Optional[Orderbook]:
        return self.orderbooks.get(token_id)

    def set_assets(self, token_ids: List[str]) -> None:
        """Replace the full subscription set (applied on next (re)connect)."""
        self._assets = {t for t in token_ids if t}

    async def add_assets(self, token_ids: List[str]) -> None:
        new = [t for t in token_ids if t and t not in self._assets]
        if not new:
            return
        self._assets.update(new)
        if self._connected and self._ws:
            await self._send_subscription(new)

    async def _send_subscription(self, assets: List[str]) -> None:
        for i in range(0, len(assets), config.WS_SUBSCRIBE_CHUNK):
            chunk = assets[i : i + config.WS_SUBSCRIBE_CHUNK]
            msg = {"assets_ids": chunk, "type": "MARKET"}
            await self._ws.send(json.dumps(msg))
            logger.info("Subscribed to %d assets (chunk)", len(chunk))

    def _emit(self, token_id: str) -> None:
        if self._on_update:
            try:
                self._on_update(token_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("on_update callback error: %s", exc)

    def _handle_message(self, data: dict) -> None:
        event_type = data.get("event_type", "")
        if event_type == "book":
            token_id = data.get("asset_id", "")
            if not token_id:
                return
            ob = self.orderbooks.setdefault(token_id, Orderbook())
            ob.apply_snapshot(data)
            self._emit(token_id)
        elif event_type == "price_change":
            for ch in data.get("price_changes", []) or data.get("changes", []):
                token_id = ch.get("asset_id", "")
                if not token_id:
                    continue
                ob = self.orderbooks.setdefault(token_id, Orderbook())
                try:
                    ob.apply_change(
                        float(ch.get("price", 0)),
                        float(ch.get("size", 0)),
                        ch.get("side", ""),
                    )
                except (TypeError, ValueError):
                    continue
                self._emit(token_id)
        # tick_size_change / last_trade_price: ignored for orderbook purposes

    async def _run_once(self) -> None:
        async with websockets.connect(
            self.url, ping_interval=20, ping_timeout=10, max_size=None
        ) as ws:
            self._ws = ws
            self._connected = True
            logger.info("WebSocket connected to %s", self.url)
            if self._assets:
                await self._send_subscription(list(self._assets))
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            self._handle_message(item)
                elif isinstance(data, dict):
                    self._handle_message(data)

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._run_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("WebSocket error: %s", exc)
            finally:
                self._connected = False
                self._ws = None
            if not self._running:
                break
            logger.info("Reconnecting websocket in 5s...")
            await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False
