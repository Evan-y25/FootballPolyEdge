"""
Startup orchestration:
  1. discover World Cup games (filtered by time window)
  2. open the CLOB market websocket and subscribe to all tokens
  3. start the aiohttp server (REST + browser websocket push)
  4. periodically re-discover games (add new / drop closed)
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from . import config, gamma
from .server import Broadcaster, build_app
from .state import AppState
from .ws_client import MarketWebSocket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("footballpolyedge")


async def discover_and_subscribe(
    state: AppState, ws: MarketWebSocket, broadcaster: Broadcaster
) -> None:
    games = await gamma.fetch_world_cup_games()
    games = gamma.filter_by_window(games, config.SUBSCRIBE_WINDOW_DAYS)
    state.set_games(games)
    broadcaster.rebuild_index()

    tokens: list[str] = []
    for g in games:
        tokens.extend(g.all_tokens())
    await ws.add_assets(tokens)
    logger.info("Tracking %d games, %d tokens", len(games), len(tokens))


async def refresh_loop(state: AppState, ws: MarketWebSocket, broadcaster: Broadcaster) -> None:
    while True:
        await asyncio.sleep(config.REFRESH_INTERVAL)
        try:
            await discover_and_subscribe(state, ws, broadcaster)
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh failed: %s", exc)


async def run() -> None:
    ws = MarketWebSocket()
    state = AppState(ws)
    broadcaster = Broadcaster(state)

    @ws.on_update
    def _on_token_update(token_id: str) -> None:
        broadcaster.mark_token(token_id)

    # Initial discovery + subscription.
    await discover_and_subscribe(state, ws, broadcaster)

    # Background tasks.
    ws_task = asyncio.create_task(ws.run(), name="ws")
    push_task = asyncio.create_task(broadcaster.push_loop(), name="push")
    refresh_task = asyncio.create_task(
        refresh_loop(state, ws, broadcaster), name="refresh"
    )

    # HTTP server.
    app = build_app(state, broadcaster)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.HOST, config.PORT)
    await site.start()
    logger.info("Server running at http://%s:%d", config.HOST, config.PORT)

    try:
        await asyncio.Event().wait()  # run forever
    finally:
        ws.stop()
        for t in (ws_task, push_task, refresh_task):
            t.cancel()
        await runner.cleanup()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
