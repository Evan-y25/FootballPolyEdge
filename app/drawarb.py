"""
Cross-market draw-arb signal builder.

For one game, returns aligned time series (focused on the in-play window) of:
  - ask(1X2 draw)            (what you pay to BUY the direct draw)
  - bid(each draw scoreline) (what you receive to SELL each synthetic component)
The frontend computes signal = sum(selected score bids) - draw_ask; whenever
signal > 0 the cross-market draw arb is live (buy direct draw, sell components,
hold to settlement -> lock the gap).
"""

from __future__ import annotations

import re
from typing import Optional

from .replay import _kickoff_ts


def _is_draw(label):
    m = re.search(r"(\d+)\s*-\s*(\d+)", label)
    return bool(m) and m.group(1) == m.group(2)


def build_draw_arb(store, slug: str, n_points: int = 700) -> Optional[dict]:
    rows = store.yes_quotes(slug)  # (ts, market, label, bid, ask)
    if not rows:
        return None
    meta = store.game_meta(slug) or {"home": "", "away": "", "kickoff": ""}

    # group: draw 1x2 ask(+size); each draw-score bid(+size)
    draw_ask_pts, draw_asz_pts = [], []
    score_bid_pts, score_bsz_pts = {}, {}
    for ts, market, label, bid, ask, bsz, asz in rows:
        if market == "1x2" and label == "draw":
            draw_ask_pts.append((ts, ask or 0.0))
            draw_asz_pts.append((ts, asz or 0.0))
        elif market == "score" and _is_draw(label):
            score_bid_pts.setdefault(label, []).append((ts, bid or 0.0))
            score_bsz_pts.setdefault(label, []).append((ts, bsz or 0.0))
    if not draw_ask_pts or not score_bid_pts:
        return None

    tmin = rows[0][0]
    tmax = rows[-1][0]
    if tmax <= tmin:
        tmax = tmin + 1
    ko = _kickoff_ts(meta.get("kickoff"))
    start = max(tmin, ko - 1800) if (ko and ko > tmin) else tmin
    if start >= tmax:
        start = tmin
    step = (tmax - start) / (n_points - 1)
    grid = [start + int(i * step) for i in range(n_points)]

    def ffill(pts):
        arr, j, last = [], 0, None
        for gt in grid:
            while j < len(pts) and pts[j][0] <= gt:
                last = pts[j][1]
                j += 1
            arr.append(round(last, 4) if last is not None else None)
        return arr

    draw_ask = ffill(draw_ask_pts)
    draw_ask_size = ffill(draw_asz_pts)
    scores = {lab: ffill(pts) for lab, pts in sorted(score_bid_pts.items())}
    scores_size = {lab: ffill(pts) for lab, pts in sorted(score_bsz_pts.items())}

    return {
        "slug": slug, "home": meta["home"], "away": meta["away"],
        "kickoff_ts": ko, "x": grid,
        "draw_ask": draw_ask, "draw_ask_size": draw_ask_size,
        "scores": scores, "scores_size": scores_size,
        "available": sorted(scores.keys()),
    }
