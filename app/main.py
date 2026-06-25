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
import pathlib
import time
from datetime import datetime, timezone

from aiohttp import web

from . import config, gamma, genome
from .arb_executor import ArbExecutor
from .autotrader import AutoTrader
from .evolve import Evolver
from .paper import PaperTrader
from .server import Broadcaster, build_app
from .state import AppState
from .store import Store
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


async def sampler_loop(state: AppState, store: Store) -> None:
    """Persist orderbook ticks (on-change) for every tracked token, for replay."""
    while True:
        await asyncio.sleep(config.SAMPLE_INTERVAL)
        try:
            now = int(time.time())
            written = 0
            for g in state.games:
                store.upsert_game(g.slug, g.home, g.away, g.kickoff, now)
                legs = [("1x2", o) for o in (g.home_win, g.draw, g.away_win) if o]
                legs += [("score", o) for o in g.scores]
                for market, o in legs:
                    for side, token in (("yes", o.yes_token), ("no", o.no_token)):
                        if not token:
                            continue
                        b = state.token_best(token)
                        if not b["live"]:
                            continue
                        if store.record_tick(now, token, g.slug, market, o.label, side,
                                             b["bid"], b["ask"], b["bid_size"], b["ask_size"]):
                            written += 1
            if written:
                store.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("sampler failed: %s", exc)


async def resolution_loop(state: AppState, store: Store, paper, evolver, arb_paper) -> None:
    """Finished games: fetch outcomes -> settle both paper books -> evolve (per-match)."""
    from .evolve import evolution_sweep
    while True:
        await asyncio.sleep(config.RESOLUTION_INTERVAL)
        try:
            out = await evolution_sweep(store, paper, evolver)
            # settle the arb book at true outcome too
            for slug in out["resolved_now"]:
                arb_paper.settle(slug, store.resolution_map(slug))
            # also settle any arb book slugs already resolved but not yet settled
            for slug in store.resolved_slugs():
                if any(p["status"] == "open" and p["slug"] == slug for p in arb_paper.positions):
                    arb_paper.settle(slug, store.resolution_map(slug))
            if out["resolved_now"] or out["evolved"]:
                logger.info("evolution sweep: %s", out)
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolution sweep failed: %s", exc)


async def run() -> None:
    ws = MarketWebSocket()
    state = AppState(ws)
    broadcaster = Broadcaster(state)
    paper_path = pathlib.Path(__file__).resolve().parent.parent / "paper_positions.json"
    paper = PaperTrader(state, paper_path, config.PAPER_START_CASH)
    auto = AutoTrader(state, paper)
    paper.auto = auto  # let paper read live auto params for exit signals
    genome.save(config.GENOME_PATH, auto.params)  # materialize the committable genome file
    pathlib.Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
    store = Store(config.DB_PATH)
    paper.store = store  # journal trades for replay
    paper.journal_all()  # recover trade history into the store (survives restarts)
    evolver = Evolver(store, auto, autocommit=config.EVOLVE_AUTOCOMMIT) if config.EVOLVE_ENABLED else None
    arb_paper = PaperTrader(state, pathlib.Path(config.DATA_DIR) / "arb_positions.json", config.ARB_BANKROLL)
    arb_paper.store = store
    arb_paper.journal_all()
    arb = ArbExecutor(state, arb_paper)
    arb.enabled = config.ARB_ENABLED
    logger.info("Store at %s | %s | evolve=%s autocommit=%s",
                config.DB_PATH, store.stats(), config.EVOLVE_ENABLED, config.EVOLVE_AUTOCOMMIT)

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
    auto_task = asyncio.create_task(auto.run(), name="autotrader")
    sampler_task = asyncio.create_task(sampler_loop(state, store), name="sampler")
    resolution_task = asyncio.create_task(
        resolution_loop(state, store, paper, evolver, arb_paper), name="resolution")
    arb_task = asyncio.create_task(arb.run(), name="arb")

    # HTTP server.
    app = build_app(state, broadcaster, paper, auto, store, evolver, arb)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.HOST, config.PORT)
    await site.start()
    logger.info("Server running at http://%s:%d", config.HOST, config.PORT)

    try:
        await asyncio.Event().wait()  # run forever
    finally:
        ws.stop()
        for t in (ws_task, push_task, refresh_task, auto_task, sampler_task, resolution_task, arb_task):
            t.cancel()
        store.close()
        await runner.cleanup()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
