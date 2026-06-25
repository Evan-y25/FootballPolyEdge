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


async def _api_paper(request: web.Request) -> web.Response:
    return web.json_response(request.app["paper"].snapshot())


async def _api_paper_open(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "无效请求体"}, status=400)
    res = request.app["paper"].open(
        slug=body.get("slug", ""),
        market=body.get("market", "score"),
        label=body.get("label", ""),
        side=body.get("side", "yes"),
        stake=float(body.get("stake", 0) or 0),
    )
    return web.json_response(res, status=200 if res.get("ok") else 400)


async def _api_paper_close(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "无效请求体"}, status=400)
    res = request.app["paper"].close(int(body.get("id", 0)))
    return web.json_response(res, status=200 if res.get("ok") else 400)


async def _api_paper_reset(request: web.Request) -> web.Response:
    return web.json_response(request.app["paper"].reset())


async def _api_auto(request: web.Request) -> web.Response:
    return web.json_response(request.app["auto"].status())


async def _api_auto_set(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "无效请求体"}, status=400)
    return web.json_response(request.app["auto"].set_enabled(bool(body.get("enabled", False))))


async def _api_evolution(request: web.Request) -> web.Response:
    from . import evolve, genome
    auto = request.app["auto"]
    return web.json_response({
        "current_genome": auto.params,
        "spec": {k: v[0] for k, v in genome.GENOME_SPEC.items()},
        "history": evolve.load_learnings(100),
    })


async def _replay_page(request: web.Request) -> web.Response:
    return web.FileResponse(FRONTEND_DIR / "replay.html")


async def _api_replay_games(request: web.Request) -> web.Response:
    store = request.app["store"]
    if store is None:
        return web.json_response({"games": []})
    return web.json_response({"games": store.replay_games()})


async def _api_replay(request: web.Request) -> web.Response:
    from . import replay
    store = request.app["store"]
    slug = request.query.get("slug", "")
    if store is None or not slug:
        return web.json_response({"error": "missing slug or store"}, status=400)
    data = replay.build_series(store, slug)
    if data is None:
        return web.json_response({"error": "no tick data for slug"}, status=404)
    return web.json_response(data)


async def _api_arb(request: web.Request) -> web.Response:
    arb = request.app.get("arb")
    if arb is None:
        return web.json_response({"enabled": False, "baskets": [], "log": []})
    return web.json_response(arb.status())


async def _api_arb_set(request: web.Request) -> web.Response:
    arb = request.app.get("arb")
    if arb is None:
        return web.json_response({"error": "arb 未初始化"}, status=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return web.json_response(arb.set_enabled(bool(body.get("enabled", False))))


async def _api_arb_scan(request: web.Request) -> web.Response:
    arb = request.app.get("arb")
    if arb is None:
        return web.json_response({"error": "arb 未初始化"}, status=400)
    res = arb.scan_once()
    return web.json_response({"ok": True, **res, "status": arb.status()})


async def _api_evolution_run(request: web.Request) -> web.Response:
    from . import evolve
    evolver = request.app.get("evolver")
    if evolver is None:
        return web.json_response({"ok": False, "error": "进化未启用 (EVOLVE_ENABLED=0)"}, status=400)
    out = await evolve.evolution_sweep(request.app["store"], request.app["paper"], evolver)
    msg = f"结算 {len(out['resolved_now'])} 场，复盘 {len(out['evolved'])} 场"
    if not out["resolved_now"] and not out["evolved"]:
        msg = "暂无可进化的已结算比赛（需要：我们交易过 + 已结算的比赛）"
    return web.json_response({"ok": True, "message": msg, **out})


async def _api_auto_params(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "无效请求体"}, status=400)
    return web.json_response(request.app["auto"].set_params(body or {}))


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


def build_app(state: AppState, broadcaster: Broadcaster, paper, auto, store=None, evolver=None, arb=None) -> web.Application:
    app = web.Application()
    app["state"] = state
    app["broadcaster"] = broadcaster
    app["paper"] = paper
    app["auto"] = auto
    app["store"] = store
    app["evolver"] = evolver
    app["arb"] = arb
    app.router.add_get("/", _index)
    app.router.add_get("/api/games", _api_games)
    app.router.add_get("/api/paper", _api_paper)
    app.router.add_post("/api/paper/open", _api_paper_open)
    app.router.add_post("/api/paper/close", _api_paper_close)
    app.router.add_post("/api/paper/reset", _api_paper_reset)
    app.router.add_get("/api/auto", _api_auto)
    app.router.add_post("/api/auto", _api_auto_set)
    app.router.add_post("/api/auto/params", _api_auto_params)
    app.router.add_get("/api/evolution", _api_evolution)
    app.router.add_post("/api/evolution/run", _api_evolution_run)
    app.router.add_get("/api/arb", _api_arb)
    app.router.add_post("/api/arb", _api_arb_set)
    app.router.add_post("/api/arb/scan", _api_arb_scan)
    app.router.add_get("/replay", _replay_page)
    app.router.add_get("/api/replay/games", _api_replay_games)
    app.router.add_get("/api/replay", _api_replay)
    app.router.add_get("/ws", _ws_handler)
    app.router.add_get("/{name:[^/]+\\.(css|js)}", _static)
    return app
