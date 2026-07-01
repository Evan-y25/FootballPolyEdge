"""
Replay series builder for the orderbook-movement visualization page.

Turns a game's stored YES-side ticks into aligned time series (implied
probability = yes mid) on a common time grid, so the frontend can plot how
1X2 and exact-score markets move over the match — and auto-detects likely
goal moments from sharp jumps in the 1X2 win probability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def _kickoff_ts(kickoff: str):
    if not kickoff:
        return None
    try:
        return int(datetime.fromisoformat(kickoff.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def build_series(store, slug: str, n_points: int = 600) -> Optional[dict]:
    ticks = store.yes_ticks(slug)
    if not ticks:
        return None
    meta = store.game_meta(slug) or {"home": "", "away": "", "kickoff": ""}

    # group ticks by (market,label)
    groups: dict = {}
    for ts, market, label, mid in ticks:
        groups.setdefault((market, label), []).append((ts, mid))

    tmin = ticks[0][0]
    tmax = ticks[-1][0]
    if tmax <= tmin:
        tmax = tmin + 1
    # Focus the grid on the in-play window (goals happen there); the long flat
    # pre-match period would otherwise eat ~all of the resolution.
    ko = _kickoff_ts(meta.get("kickoff"))
    start = max(tmin, ko - 1800) if (ko and ko > tmin) else tmin
    if start >= tmax:
        start = tmin
    step = (tmax - start) / (n_points - 1)
    grid = [start + int(i * step) for i in range(n_points)]

    def forward_fill(points):
        arr, j, last = [], 0, None
        for gt in grid:
            while j < len(points) and points[j][0] <= gt:
                last = points[j][1]
                j += 1
            arr.append(round(last, 4) if last is not None else None)
        return arr

    # markets: {market_key: {label: series}}
    markets: dict = {}
    for (market, label), pts in groups.items():
        markets.setdefault(market, {})[label] = forward_fill(pts)

    onex2 = markets.get("1x2", {})
    scores = markets.get("score", {})

    # ordered group metadata for the frontend selector
    from .gamma import GROUP_TITLES
    order = ["1x2", "score", "team_to_advance", "spread", "totals", "team_totals",
             "btts", "first_to_score", "halves", "extra_time", "penalty", "more_other"]
    present = sorted(markets.keys(), key=lambda k: order.index(k) if k in order else 99)
    group_meta = [{"key": k, "title": GROUP_TITLES.get(k, k),
                   "labels": sorted(markets[k].keys())} for k in present]

    step_sec = max(1.0, (grid[-1] - grid[0]) / (n_points - 1))
    goals = _detect_goals(grid, onex2, ko, step_sec)
    res = {f"{m}|{l}": w for (m, l), w in store.resolution_map(slug).items()}

    return {
        "slug": slug, "home": meta["home"], "away": meta["away"],
        "kickoff": meta.get("kickoff"), "kickoff_ts": _kickoff_ts(meta.get("kickoff")),
        "resolution": res,
        "x": grid, "onex2": onex2, "scores": scores,
        "markets": markets, "groups": group_meta, "goals": goals,
    }


def _detect_goals(grid, onex2, kickoff_ts, step_sec, jump=0.08):
    """Mark timestamps where home-win prob makes a sustained sharp move (~a goal).
    Window ~2 min; dedup within ~3 min so one swing isn't double-counted."""
    home = onex2.get("home")
    if not home:
        return []
    window = max(2, round(120 / step_sec))
    dedup = max(120, int(3 * 60))
    goals, last_mark = [], -10_000
    for i in range(window, len(grid)):
        a, b = home[i], home[i - window]
        if a is None or b is None:
            continue
        if kickoff_ts and grid[i] < kickoff_ts:  # only in-play
            continue
        d = a - b
        if abs(d) >= jump and grid[i] - last_mark > dedup:
            goals.append({"ts": grid[i], "delta": round(d, 3),
                          "dir": "home" if d > 0 else "away"})
            last_mark = grid[i]
    return goals
