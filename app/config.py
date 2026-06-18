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
