"""
Gamma API client — World Cup game discovery.

Discovers all World Cup games from Polymarket's Gamma API, pairing each
"1X2" event (slug `fifwc-{home}-{away}-{date}`) with its matching
"Exact Score" event (same slug + `-exact-score`), and parses the CLOB
token ids needed for the market websocket subscription.

See DESIGN.md §2 for the data-model research notes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from . import config

logger = logging.getLogger(__name__)

EXACT_SCORE_SUFFIX = "-exact-score"


def _event_kind(event: Dict[str, Any]) -> Tuple[str, str]:
    """
    Classify an event. Returns (kind, base_slug); kind in {"1x2", "score", "other"}.

    The exact-score event reliably ends with `-exact-score` in its slug.
    A *base* 1X2 game's title is just "A vs. B" with NO " - {qualifier}" suffix;
    derivatives ("- Halftime Result", "- Total Corners", "- More Markets", ...)
    always carry a " - " qualifier and are skipped. (Slug date position is
    inconsistent across derivatives, so the title is the reliable signal.)
    """
    slug = event.get("slug", "")
    title = event.get("title", "")
    if slug.endswith(EXACT_SCORE_SUFFIX):
        return "score", slug[: -len(EXACT_SCORE_SUFFIX)]
    if " vs" in title and " - " not in title:
        return "1x2", slug
    return "other", slug


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Outcome:
    """A single binary (Yes/No) market with its CLOB token ids."""

    market_id: str
    label: str            # "home" | "draw" | "away" for 1X2, or score label
    question: str
    yes_token: str
    no_token: str
    init_yes: float = 0.0  # initial price (fallback before websocket data)
    init_no: float = 0.0


@dataclass
class Game:
    """A World Cup match: a 1X2 triple plus an exact-score book."""

    slug: str
    home: str
    away: str
    kickoff: str                       # ISO8601 string
    home_win: Optional[Outcome] = None
    draw: Optional[Outcome] = None
    away_win: Optional[Outcome] = None
    scores: List[Outcome] = field(default_factory=list)

    @property
    def onex2(self) -> List[Outcome]:
        return [o for o in (self.home_win, self.draw, self.away_win) if o]

    def all_tokens(self) -> List[str]:
        tokens: List[str] = []
        for o in self.onex2:
            tokens.extend([o.yes_token, o.no_token])
        for o in self.scores:
            tokens.extend([o.yes_token, o.no_token])
        return [t for t in tokens if t]

    def kickoff_dt(self) -> Optional[datetime]:
        return _parse_dt(self.kickoff)

    def status(self, now: Optional[datetime] = None) -> str:
        now = now or datetime.now(timezone.utc)
        ko = self.kickoff_dt()
        if ko is None:
            return "upcoming"
        return "live" if now >= ko else "upcoming"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _parse_json_field(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _tokens(market: Dict[str, Any]) -> Tuple[str, str]:
    ids = _parse_json_field(market.get("clobTokenIds"), [])
    yes = ids[0] if len(ids) > 0 else ""
    no = ids[1] if len(ids) > 1 else ""
    return str(yes), str(no)


def _prices(market: Dict[str, Any]) -> Tuple[float, float]:
    prices = _parse_json_field(market.get("outcomePrices"), ["0", "0"])
    try:
        yes = float(prices[0]) if len(prices) > 0 else 0.0
        no = float(prices[1]) if len(prices) > 1 else 0.0
    except (TypeError, ValueError):
        yes, no = 0.0, 0.0
    return yes, no


def _classify_1x2(group_title: str, question: str, home: str, away: str) -> str:
    """Return 'home' | 'draw' | 'away' for a 1X2 market."""
    gt = (group_title or "").lower()
    if gt.startswith("draw") or "draw" in (question or "").lower():
        return "draw"
    # groupItemTitle is the team name for win markets.
    if _name_match(group_title, home):
        return "home"
    if _name_match(group_title, away):
        return "away"
    return "unknown"


def _name_match(a: str, b: str) -> bool:
    """Loose team-name match (handles 'Bosnia and Herzegovina' vs 'Bosnia-Herzegovina')."""
    na = _norm_name(a)
    nb = _norm_name(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def _norm_name(s: str) -> str:
    s = (s or "").lower()
    for ch in "-.,":
        s = s.replace(ch, " ")
    s = s.replace(" and ", " ")
    return " ".join(s.split())


def _split_title(title: str) -> Tuple[str, str]:
    """'Czechia vs. South Africa' -> ('Czechia', 'South Africa')."""
    for sep in (" vs. ", " vs ", " - "):
        if sep in title:
            left, right = title.split(sep, 1)
            return left.strip(), right.strip()
    return title.strip(), ""


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
async def _fetch_events(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    """Fetch all non-closed events for the configured series, paginated."""
    events: List[Dict[str, Any]] = []
    offset = 0
    limit = 100
    while True:
        url = (
            f"{config.GAMMA_HOST}/events"
            f"?series_id={config.SERIES_ID}&closed=false&limit={limit}&offset={offset}"
        )
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                logger.warning("Gamma events fetch failed: HTTP %s", resp.status)
                break
            batch = await resp.json()
        if not isinstance(batch, list) or not batch:
            break
        events.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return events


def _build_1x2(event: Dict[str, Any]) -> Optional[Game]:
    title = event.get("title", "")
    home, away = _split_title(title)
    kickoff = event.get("startTime") or event.get("endDate") or event.get("eventDate", "")
    game = Game(slug=event.get("slug", ""), home=home, away=away, kickoff=kickoff)

    for m in event.get("markets", []):
        if m.get("closed"):
            continue
        yes, no = _tokens(m)
        if not yes:
            continue
        iy, ino = _prices(m)
        leg = _classify_1x2(m.get("groupItemTitle", ""), m.get("question", ""), home, away)
        outcome = Outcome(
            market_id=str(m.get("id", "")),
            label=leg,
            question=m.get("question", ""),
            yes_token=yes,
            no_token=no,
            init_yes=iy,
            init_no=ino,
        )
        if leg == "home":
            game.home_win = outcome
        elif leg == "draw":
            game.draw = outcome
        elif leg == "away":
            game.away_win = outcome

    if not (game.home_win or game.draw or game.away_win):
        return None
    return game


def _attach_scores(game: Game, event: Dict[str, Any]) -> None:
    for m in event.get("markets", []):
        if m.get("closed"):
            continue
        yes, no = _tokens(m)
        if not yes:
            continue
        iy, ino = _prices(m)
        label = _score_label(m.get("groupItemTitle", ""), game.home, game.away)
        game.scores.append(
            Outcome(
                market_id=str(m.get("id", "")),
                label=label,
                question=m.get("question", ""),
                yes_token=yes,
                no_token=no,
                init_yes=iy,
                init_no=ino,
            )
        )


def _score_label(group_title: str, home: str, away: str) -> str:
    """'Czechia 1 - 0 South Africa' -> '1 - 0'; keep 'Any Other Score' readable."""
    gt = group_title or ""
    if "any other" in gt.lower():
        return "Other"
    # Strip leading home name and trailing away name, keep the score core.
    core = gt
    if home and core.startswith(home):
        core = core[len(home):]
    if away and core.endswith(away):
        core = core[: -len(away)]
    return core.strip() or gt


async def fetch_world_cup_games() -> List[Game]:
    """Discover all World Cup games, pairing 1X2 with exact-score events."""
    async with aiohttp.ClientSession() as session:
        events = await _fetch_events(session)

    onex2_events: Dict[str, Dict[str, Any]] = {}
    score_events: Dict[str, Dict[str, Any]] = {}
    for e in events:
        kind, base = _event_kind(e)
        if kind == "1x2":
            onex2_events[base] = e
        elif kind == "score":
            score_events[base] = e
        # "other" (halftime-result, etc.) is skipped

    games: List[Game] = []
    for slug, event in onex2_events.items():
        game = _build_1x2(event)
        if not game:
            continue
        score_event = score_events.get(slug)
        if score_event:
            _attach_scores(game, score_event)
        games.append(game)

    games.sort(key=lambda g: g.kickoff or "")
    logger.info(
        "Discovered %d World Cup games (%d with exact-score)",
        len(games),
        sum(1 for g in games if g.scores),
    )
    return games


async def _fetch_event_by_slug(session: aiohttp.ClientSession, slug: str) -> Optional[Dict[str, Any]]:
    url = f"{config.GAMMA_HOST}/events/slug/{slug}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except Exception:  # noqa: BLE001
        return None


def _market_winner(market: Dict[str, Any]) -> Optional[str]:
    """Resolved binary market: 'yes' if YES settled to 1, 'no' if NO did, else None."""
    if not market.get("closed"):
        return None
    prices = _parse_json_field(market.get("outcomePrices"), None)
    if not prices or len(prices) < 2:
        return None
    try:
        yes, no = float(prices[0]), float(prices[1])
    except (TypeError, ValueError):
        return None
    if yes >= 0.99:
        return "yes"
    if no >= 0.99:
        return "no"
    return None


async def fetch_resolution(base_slug: str) -> Optional[dict]:
    """
    Fetch settled outcomes for a finished game (1X2 + exact-score events).
    Returns {"resolved": bool, "rows": [(market,label,yes_token,no_token,winner), ...]}
    or None if the events can't be fetched. resolved=False means not settled yet.
    """
    async with aiohttp.ClientSession() as session:
        ev_1x2 = await _fetch_event_by_slug(session, base_slug)
        ev_score = await _fetch_event_by_slug(session, base_slug + EXACT_SCORE_SUFFIX)

    if not ev_1x2 and not ev_score:
        return None

    rows: List[tuple] = []
    any_open = False

    if ev_1x2:
        home, away = _split_title(ev_1x2.get("title", ""))
        for m in ev_1x2.get("markets", []):
            yes, no = _tokens(m)
            leg = _classify_1x2(m.get("groupItemTitle", ""), m.get("question", ""), home, away)
            winner = _market_winner(m)
            if winner is None and not m.get("closed"):
                any_open = True
            rows.append(("1x2", leg, yes, no, winner or "unknown"))

    if ev_score:
        home, away = _split_title(ev_score.get("title", "").replace(" - Exact Score", ""))
        for m in ev_score.get("markets", []):
            yes, no = _tokens(m)
            label = _score_label(m.get("groupItemTitle", ""), home, away)
            winner = _market_winner(m)
            if winner is None and not m.get("closed"):
                any_open = True
            rows.append(("score", label, yes, no, winner or "unknown"))

    resolved = bool(rows) and not any_open and all(r[4] != "unknown" for r in rows)
    return {"resolved": resolved, "rows": rows}


def filter_by_window(games: List[Game], days: int) -> List[Game]:
    """Keep games kicking off within `days` (plus already-live games). days<=0 -> all."""
    if days <= 0:
        return games
    now = datetime.now(timezone.utc)
    horizon_secs = days * 86400
    kept: List[Game] = []
    for g in games:
        ko = g.kickoff_dt()
        if ko is None:
            kept.append(g)
            continue
        delta = (ko - now).total_seconds()
        # live (delta<0) or starting within the window
        if delta <= horizon_secs:
            kept.append(g)
    return kept
