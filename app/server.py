"""
aiohttp web server:
  GET /            -> frontend page
  GET /styles.css, /app.js -> static assets
  GET /api/games   -> full JSON snapshot
  WS  /ws          -> snapshot on connect, then throttled incremental updates
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Dict, Set

from aiohttp import WSMsgType, web

from . import config
from .state import AppState

logger = logging.getLogger(__name__)

FRONTEND_DIR = pathlib.Path(__file__).resolve().parent.parent / "frontend"


class Broadcaster:
    """Tracks browser websocket clients and pushes throttled game updates."""

    def __init__(self, state: AppState) -> None:
        self.state = state
        self.clients: Set[web.WebSocketResponse] = set()
        self._dirty: Set[str] = set()
        self._token_index: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    def rebuild_index(self) -> None:
        self._token_index = self.state.token_to_slug()

    def mark_token(self, token_id: str) -> None:
        slug = self._token_index.get(token_id)
        if slug:
            self._dirty.add(slug)

    async def register(self, ws: web.WebSocketResponse) -> None:
        self.clients.add(ws)
        await ws.send_json(self.state.snapshot())

    def unregister(self, ws: web.WebSocketResponse) -> None:
        self.clients.discard(ws)

    async def push_loop(self) -> None:
        interval = config.PUSH_THROTTLE_MS / 1000.0
        while True:
            await asyncio.sleep(interval)
            if not self._dirty or not self.clients:
                self._dirty.clear()
                continue
            slugs = list(self._dirty)
            self._dirty.clear()
            games = [g for g in (self.state.game_dict(s) for s in slugs) if g]
            if not games:
                continue
            frame = json.dumps(
                {
                    "type": "update",
                    "updated_at": self.state.snapshot_meta_time(),
                    "ws_connected": self.state.ws.is_connected,
                    "subscribed_tokens": self.state.ws.subscribed_count,
                    "games": games,
                }
            )
            dead = []
            for ws in self.clients:
                try:
                    await ws.send_str(frame)
                except (ConnectionResetError, RuntimeError):
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)


async def _index(request: web.Request) -> web.Response:
    return web.FileResponse(FRONTEND_DIR / "index.html")


async def _static(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    path = FRONTEND_DIR / name
    if not path.is_file() or path.parent != FRONTEND_DIR:
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def _api_games(request: web.Request) -> web.Response:
    state: AppState = request.app["state"]
    return web.json_response(state.snapshot())


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    broadcaster: Broadcaster = request.app["broadcaster"]
    await broadcaster.register(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        broadcaster.unregister(ws)
    return ws


def build_app(state: AppState, broadcaster: Broadcaster) -> web.Application:
    app = web.Application()
    app["state"] = state
    app["broadcaster"] = broadcaster
    app.router.add_get("/", _index)
    app.router.add_get("/api/games", _api_games)
    app.router.add_get("/ws", _ws_handler)
    app.router.add_get("/{name:[^/]+\\.(css|js)}", _static)
    return app
