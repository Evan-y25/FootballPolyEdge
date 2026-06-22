"""
SQLite persistence for replay / backtest / self-evolution.

Stores the raw market data and trade history needed to replay a day and score
the strategy under any genome later:

  ticks        time series of best bid/ask per token (written on-change)
  games        match metadata (slug, teams, kickoff)
  resolutions  per-market settled outcome (winner yes/no), fetched from Gamma
  trades       paper-trade journal (open/add/close with reason + pnl)
  evolution    log of every evolution attempt (genome, score, adopted?)

Self-contained: its own file (DATA_DIR/market.db), WAL mode, never touches the
WorldCup project's Postgres. All writes go through a single lock (writes are
infrequent — a sampler tick every ~15s).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    ts         INTEGER NOT NULL,
    token_id   TEXT NOT NULL,
    slug       TEXT NOT NULL,
    market     TEXT NOT NULL,         -- '1x2' | 'score'
    label      TEXT NOT NULL,         -- home/draw/away or score label
    side       TEXT NOT NULL,         -- 'yes' | 'no'
    bid        REAL, ask REAL, bid_size REAL, ask_size REAL
);
CREATE INDEX IF NOT EXISTS idx_ticks_slug_ts ON ticks(slug, ts);
CREATE INDEX IF NOT EXISTS idx_ticks_token_ts ON ticks(token_id, ts);

CREATE TABLE IF NOT EXISTS games (
    slug       TEXT PRIMARY KEY,
    home       TEXT, away TEXT, kickoff TEXT,
    first_seen INTEGER, last_seen INTEGER
);

CREATE TABLE IF NOT EXISTS resolutions (
    slug       TEXT NOT NULL,
    market     TEXT NOT NULL,
    label      TEXT NOT NULL,
    yes_token  TEXT, no_token TEXT,
    winner     TEXT,                  -- 'yes' | 'no' | 'unknown'
    resolved_at INTEGER,
    PRIMARY KEY (slug, market, label)
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY,
    slug        TEXT, home TEXT, away TEXT,
    market      TEXT, label TEXT, side TEXT, token TEXT,
    entry_price REAL, shares REAL, stake REAL,
    opened_at   INTEGER, status TEXT, added INTEGER,
    close_price REAL, realized_pnl REAL, closed_at INTEGER, close_reason TEXT
);

CREATE TABLE IF NOT EXISTS evolution (
    ts          INTEGER PRIMARY KEY,
    generation  INTEGER,
    genome      TEXT,
    score       REAL,
    baseline    REAL,
    adopted     INTEGER,
    note        TEXT
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._last_tick: Dict[str, Tuple] = {}  # token -> last (bid,ask,bsz,asz)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- ticks ----------------------------------------------------------
    def record_tick(self, ts: int, token_id: str, slug: str, market: str,
                    label: str, side: str, bid, ask, bid_size, ask_size) -> bool:
        """Write a tick only if the quote changed since last stored. Returns True if written."""
        key = (round(bid or 0, 4), round(ask or 0, 4), round(bid_size or 0, 1), round(ask_size or 0, 1))
        if self._last_tick.get(token_id) == key:
            return False
        self._last_tick[token_id] = key
        with self._lock:
            self._conn.execute(
                "INSERT INTO ticks(ts,token_id,slug,market,label,side,bid,ask,bid_size,ask_size)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts, token_id, slug, market, label, side, bid, ask, bid_size, ask_size),
            )
        return True

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    # ---- games ----------------------------------------------------------
    def upsert_game(self, slug: str, home: str, away: str, kickoff: str, ts: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO games(slug,home,away,kickoff,first_seen,last_seen) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(slug) DO UPDATE SET last_seen=excluded.last_seen,"
                " home=excluded.home, away=excluded.away, kickoff=excluded.kickoff",
                (slug, home, away, kickoff, ts, ts),
            )
            self._conn.commit()

    def known_game_slugs(self) -> List[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT slug FROM games").fetchall()]

    # ---- resolutions ----------------------------------------------------
    def record_resolution(self, slug: str, market: str, label: str,
                          yes_token: str, no_token: str, winner: str, ts: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO resolutions(slug,market,label,yes_token,no_token,winner,resolved_at)"
                " VALUES(?,?,?,?,?,?,?) ON CONFLICT(slug,market,label) DO UPDATE SET"
                " winner=excluded.winner, resolved_at=excluded.resolved_at",
                (slug, market, label, yes_token, no_token, winner, ts),
            )
            self._conn.commit()

    def resolved_slugs(self) -> set:
        with self._lock:
            return {r[0] for r in self._conn.execute("SELECT DISTINCT slug FROM resolutions").fetchall()}

    def resolution_map(self, slug: str) -> Dict[Tuple[str, str], str]:
        """(market,label) -> winner('yes'/'no') for a resolved game."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT market,label,winner FROM resolutions WHERE slug=?", (slug,)
            ).fetchall()
        return {(m, l): w for m, l, w in rows}

    # ---- trades ---------------------------------------------------------
    def record_trade(self, pos: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO trades(id,slug,home,away,market,label,side,token,entry_price,shares,"
                "stake,opened_at,status,added,close_price,realized_pnl,closed_at,close_reason)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET entry_price=excluded.entry_price,"
                " shares=excluded.shares, stake=excluded.stake, status=excluded.status,"
                " added=excluded.added, close_price=excluded.close_price,"
                " realized_pnl=excluded.realized_pnl, closed_at=excluded.closed_at,"
                " close_reason=excluded.close_reason",
                (pos["id"], pos["slug"], pos.get("home"), pos.get("away"), pos["market"],
                 pos["label"], pos["side"], pos.get("token"), pos["entry_price"], pos["shares"],
                 pos["stake"], pos["opened_at"], pos["status"], 1 if pos.get("added") else 0,
                 pos.get("close_price"), pos.get("realized_pnl"), pos.get("closed_at"),
                 pos.get("close_reason")),
            )
            self._conn.commit()

    # ---- evolution ------------------------------------------------------
    def record_evolution(self, ts: int, generation: int, genome: dict, score: float,
                         baseline: float, adopted: bool, note: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO evolution(ts,generation,genome,score,baseline,adopted,note)"
                " VALUES(?,?,?,?,?,?,?)",
                (ts, generation, json.dumps(genome, ensure_ascii=False), score, baseline,
                 1 if adopted else 0, note),
            )
            self._conn.commit()

    # ---- replay helpers (used by backtest) ------------------------------
    def ticks_for_slug(self, slug: str) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,token_id,market,label,side,bid,ask,bid_size,ask_size"
                " FROM ticks WHERE slug=? ORDER BY ts", (slug,)
            ).fetchall()
        cols = ["ts", "token_id", "market", "label", "side", "bid", "ask", "bid_size", "ask_size"]
        return [dict(zip(cols, r)) for r in rows]

    def trades_for_slug(self, slug: str) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,slug,home,away,market,label,side,entry_price,shares,stake,"
                "status,added,close_price,realized_pnl,close_reason FROM trades WHERE slug=?",
                (slug,),
            ).fetchall()
        cols = ["id", "slug", "home", "away", "market", "label", "side", "entry_price",
                "shares", "stake", "status", "added", "close_price", "realized_pnl", "close_reason"]
        return [dict(zip(cols, r)) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            c = self._conn
            return {
                "ticks": c.execute("SELECT COUNT(*) FROM ticks").fetchone()[0],
                "games": c.execute("SELECT COUNT(*) FROM games").fetchone()[0],
                "resolved_games": c.execute("SELECT COUNT(DISTINCT slug) FROM resolutions").fetchone()[0],
                "trades": c.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
            }
