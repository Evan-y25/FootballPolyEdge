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
MORE_MARKETS_SUFFIX = "-more-markets"
FIRST_SCORE_SUFFIX = "-first-to-score"


def _event_kind(event: Dict[str, Any]) -> Tuple[str, str]:
    """
    Classify an event. Returns (kind, base_slug); kind in
    {"1x2", "score", "more", "first_score", "other"}.

    Derivative events share the base slug plus a suffix. We capture the base
    1X2, exact-score, the big "More Markets" bundle (spread/totals/BTTS/…), and
    First-to-Score. Others (halftime, corners, player props) are skipped.
    """
    slug = event.get("slug", "")
    title = event.get("title", "")
    if slug.endswith(EXACT_SCORE_SUFFIX):
        return "score", slug[: -len(EXACT_SCORE_SUFFIX)]
    if slug.endswith(MORE_MARKETS_SUFFIX):
        return "more", slug[: -len(MORE_MARKETS_SUFFIX)]
    if slug.endswith(FIRST_SCORE_SUFFIX):
        return "first_score", slug[: -len(FIRST_SCORE_SUFFIX)]
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
    neg_risk: bool = False  # neg-risk market -> different exchange contract for orders


@dataclass
class MarketGroup:
    """A named group of related outcomes (e.g. all Spread lines, all Totals)."""

    key: str                # stable id used as the tick `market` value
    title: str              # display name
    outcomes: List[Outcome] = field(default_factory=list)


@dataclass
class Game:
    """A World Cup match: a 1X2 triple, an exact-score book, and extra market
    groups (spread / totals / BTTS / first-to-score / …) captured for replay."""

    slug: str
    home: str
    away: str
    kickoff: str                       # ISO8601 string
    home_win: Optional[Outcome] = None
    draw: Optional[Outcome] = None
    away_win: Optional[Outcome] = None
    scores: List[Outcome] = field(default_factory=list)
    extra: List[MarketGroup] = field(default_factory=list)

    @property
    def onex2(self) -> List[Outcome]:
        return [o for o in (self.home_win, self.draw, self.away_win) if o]

    def all_tokens(self) -> List[str]:
        tokens: List[str] = []
        for o in self.onex2:
            tokens.extend([o.yes_token, o.no_token])
        for o in self.scores:
            tokens.extend([o.yes_token, o.no_token])
        for grp in self.extra:
            for o in grp.outcomes:
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
            neg_risk=bool(m.get("negRisk") or m.get("negRiskMarketID")),
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
                neg_risk=bool(m.get("negRisk") or m.get("negRiskMarketID")),
            )
        )


# Display order + titles for the extra market groups.
_MORE_GROUP_ORDER = ["team_to_advance", "spread", "totals", "team_totals",
                     "btts", "halves", "extra_time", "penalty", "more_other"]
GROUP_TITLES = {
    "1x2": "1X2 胜平负", "score": "波胆 Exact Score",
    "team_to_advance": "晋级 Team to Advance", "spread": "让分 Spread",
    "totals": "总分 Total Goals", "team_totals": "各队总分 Team Totals",
    "btts": "双方进球 BTTS", "halves": "上下半场 Halves",
    "extra_time": "加时 Extra Time", "penalty": "点球大战 Penalty",
    "first_to_score": "首个进球 First to Score", "more_other": "其他 Other",
}


def _more_group(question: str, git: str, home: str, away: str) -> Tuple[str, str]:
    """Classify one 'More Markets' market -> (group_key, label)."""
    g = git or ""
    gl = g.lower()
    ql = (question or "").lower()
    if "team to advance" in gl:
        return "team_to_advance", (g or "Advance")
    if ql.startswith("spread:"):
        return "spread", g                       # "England (-1.5)"
    if "penalty shootout" in gl:
        return "penalty", "Yes"
    if "extra time" in gl:
        return "extra_time", "Yes"
    if "1st half" in gl or "2nd half" in gl or "first half" in gl or "second half" in gl:
        return "halves", g
    if "both teams to score" in gl:
        return "btts", "Yes"
    if "o/u" in gl:
        if (home and g.startswith(home)) or (away and g.startswith(away)):
            return "team_totals", g              # "England O/U 1.5"
        return "totals", g                       # "O/U 2.5"
    return "more_other", g


def _attach_more(game: Game, event: Dict[str, Any]) -> None:
    groups: Dict[str, MarketGroup] = {}
    for m in event.get("markets", []):
        if m.get("closed"):
            continue
        yes, no = _tokens(m)
        if not yes:
            continue
        iy, ino = _prices(m)
        key, label = _more_group(m.get("question", ""), m.get("groupItemTitle", ""),
                                 game.home, game.away)
        grp = groups.get(key)
        if grp is None:
            grp = MarketGroup(key=key, title=GROUP_TITLES.get(key, key))
            groups[key] = grp
        grp.outcomes.append(Outcome(
            market_id=str(m.get("id", "")), label=label, question=m.get("question", ""),
            yes_token=yes, no_token=no, init_yes=iy, init_no=ino,
            neg_risk=bool(m.get("negRisk") or m.get("negRiskMarketID")),
        ))
    game.extra.extend(sorted(
        groups.values(),
        key=lambda gp: _MORE_GROUP_ORDER.index(gp.key) if gp.key in _MORE_GROUP_ORDER else 99,
    ))


def _attach_first_score(game: Game, event: Dict[str, Any]) -> None:
    grp = MarketGroup(key="first_to_score", title=GROUP_TITLES["first_to_score"])
    for m in event.get("markets", []):
        if m.get("closed"):
            continue
        yes, no = _tokens(m)
        if not yes:
            continue
        iy, ino = _prices(m)
        grp.outcomes.append(Outcome(
            market_id=str(m.get("id", "")), label=m.get("groupItemTitle", "") or "?",
            question=m.get("question", ""), yes_token=yes, no_token=no,
            init_yes=iy, init_no=ino,
            neg_risk=bool(m.get("negRisk") or m.get("negRiskMarketID")),
        ))
    if grp.outcomes:
        game.extra.append(grp)


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
    more_events: Dict[str, Dict[str, Any]] = {}
    first_events: Dict[str, Dict[str, Any]] = {}
    for e in events:
        kind, base = _event_kind(e)
        if kind == "1x2":
            onex2_events[base] = e
        elif kind == "score":
            score_events[base] = e
        elif kind == "more":
            more_events[base] = e
        elif kind == "first_score":
            first_events[base] = e
        # "other" (halftime-result, corners, player props, …) is skipped

    games: List[Game] = []
    for slug, event in onex2_events.items():
        game = _build_1x2(event)
        if not game:
            continue
        if score_events.get(slug):
            _attach_scores(game, score_events[slug])
        if more_events.get(slug):
            _attach_more(game, more_events[slug])
        if first_events.get(slug):
            _attach_first_score(game, first_events[slug])
        games.append(game)

    games.sort(key=lambda g: g.kickoff or "")
    logger.info(
        "Discovered %d World Cup games (%d w/ exact-score, %d w/ extra markets)",
        len(games),
        sum(1 for g in games if g.scores),
        sum(1 for g in games if g.extra),
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
