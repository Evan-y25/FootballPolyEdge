"""Configuration loaded from environment variables with sane defaults."""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# HTTP server
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = _int("PORT", 8080)

# Polymarket Gamma API
GAMMA_HOST = os.environ.get("GAMMA_HOST", "https://gamma-api.polymarket.com")
# FIFA World Cup series id (sport "fifwc"); see DESIGN.md §2
SERIES_ID = _int("SERIES_ID", 11433)

# CLOB market websocket
WSS_MARKET_URL = os.environ.get(
    "WSS_MARKET_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"
)

# Only subscribe to games kicking off within this many days (plus live games).
# Set to 0 to subscribe to ALL non-closed games.
SUBSCRIBE_WINDOW_DAYS = _int("SUBSCRIBE_WINDOW_DAYS", 3)

# How often (seconds) to re-discover games from Gamma (add new / drop closed).
REFRESH_INTERVAL = _int("REFRESH_INTERVAL", 60)

# Max asset ids per websocket subscribe message (chunking).
WS_SUBSCRIBE_CHUNK = _int("WS_SUBSCRIBE_CHUNK", 400)

# Throttle for pushing updates to the browser (milliseconds).
PUSH_THROTTLE_MS = _int("PUSH_THROTTLE_MS", 250)


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---- Score-matrix value model (SCORE_MATRIX.md) ----
# Goal model: "dixon_coles" | "poisson" | "bivariate"(not yet -> falls back).
SCORE_MODEL = os.environ.get("SCORE_MODEL", "dixon_coles")
# Dixon-Coles low-score correlation (global calibration value).
SCORE_RHO = _float("SCORE_RHO", -0.13)
# De-vig method: "proportional" | "power".
DEVIG_METHOD = os.environ.get("DEVIG_METHOD", "proportional")
# Minimum |edge| to flag a value opportunity.
VALUE_EDGE_THRESHOLD = _float("VALUE_EDGE_THRESHOLD", 0.02)

# ---- Paper trading (模拟盘) ----
PAPER_START_CASH = _float("PAPER_START_CASH", 100.0)
PAPER_STOP_LOSS = _float("PAPER_STOP_LOSS", 0.5)  # -50% -> stop-loss signal (manual panel)

# ---- Persistence / replay (SQLite) ----
import pathlib as _pathlib

DATA_DIR = os.environ.get("DATA_DIR", str(_pathlib.Path(__file__).resolve().parent.parent / "data"))
DB_PATH = os.environ.get("DB_PATH", str(_pathlib.Path(DATA_DIR) / "market.db"))
SAMPLE_INTERVAL = _int("SAMPLE_INTERVAL", 15)        # seconds between tick samples
RESOLUTION_INTERVAL = _int("RESOLUTION_INTERVAL", 600)  # seconds between resolution sweeps
# Strategy genome (committed to git on each adopted evolution).
GENOME_PATH = os.environ.get("GENOME_PATH", str(_pathlib.Path(__file__).resolve().parent.parent / "genome.json"))
EVOLVE_ENABLED = os.environ.get("EVOLVE_ENABLED", "0") in ("1", "true", "True")
EVOLVE_AUTOCOMMIT = os.environ.get("EVOLVE_AUTOCOMMIT", "0") in ("1", "true", "True")

# ---- Auto-trader (自动交易，仅模拟盘) ----
AUTO_INTERVAL = _int("AUTO_INTERVAL", 5)              # scan interval seconds
AUTO_EDGE_THRESHOLD = _float("AUTO_EDGE_THRESHOLD", 0.03)   # min edge to open
AUTO_MIN_PRICE = _float("AUTO_MIN_PRICE", 0.50)       # min execution price (做大概率事件)
AUTO_TAKE_PROFIT = _float("AUTO_TAKE_PROFIT", 0.10)   # +10% -> close
AUTO_STOP_LOSS = _float("AUTO_STOP_LOSS", 0.30)       # -30% -> close
AUTO_MAX_POSITIONS = _int("AUTO_MAX_POSITIONS", 20)
AUTO_MAX_PER_GAME = _int("AUTO_MAX_PER_GAME", 3)
AUTO_MAX_EXPOSURE = _float("AUTO_MAX_EXPOSURE", 0.95) # fraction of start cash
AUTO_FORCE_CLOSE_MIN = _int("AUTO_FORCE_CLOSE_MIN", 10)  # force-close N min before kickoff
AUTO_MIN_STAKE = _float("AUTO_MIN_STAKE", 1.0)        # skip dust orders
# Skip wide-spread "mirage" opportunities: (ask-bid)/ask must be <= this.
# A wide spread means you enter at ask but can only exit at a much lower bid,
# so the model edge is unrealizable and would instantly trip the stop-loss.
AUTO_MAX_SPREAD = _float("AUTO_MAX_SPREAD", 0.08)
# After closing a position, don't re-open the same (slug,label,side) for N seconds.
AUTO_REENTRY_COOLDOWN = _int("AUTO_REENTRY_COOLDOWN", 300)
# Hybrid exit: a "buy NO" position entered at >= this price is a high-probability
# longshot-fade whose payoff is realized at settlement -> HOLD to settlement
# (no convergence/take-profit/pre-kickoff exit; only stop-loss + settlement).
# Other positions use the convergence/take-profit/pre-kickoff exits.
AUTO_HOLD_TO_SETTLE_PRICE = _float("AUTO_HOLD_TO_SETTLE_PRICE", 0.80)
# Average-down: if a held position's entry price falls by >= this fraction AND the
# instrument still meets all entry conditions at the new price, add ONE more tranche
# (once per position). Captures the "大概率事件一度发生 -> NO 暴跌 -> 仍便宜" dip.
AUTO_ADD_DROP = _float("AUTO_ADD_DROP", 0.10)
