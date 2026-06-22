"""
Score-matrix model — derive a "true" correct-score (波胆) probability matrix
from the 1X2 market, then compare it against Polymarket's exact-score book to
surface model-based *value* edges.

This implements SCORE_MATRIX.md (the reverse of DESIGN.md §6.3):
  1X2 mids --de-vig--> fair (P_H*, P_D*, P_A*)
          --fit--> goal model (λH, λA[, ρ])
          --expand--> full score matrix P(i,j)
          --bucketize--> Polymarket's 17 buckets
          --compare--> model vs market diff + value edges

IMPORTANT: these edges depend on the Poisson / Dixon-Coles assumptions and are
*model value*, NOT risk-free arbitrage (see SCORE_MATRIX.md §9). The 1X2 only
constrains 2 degrees of freedom, so the matrix interior is a model extrapolation.

Pure-Python (no scipy/numpy dependency): a 2D damped-Newton solver with a
grid-search fallback. K=10 truncation keeps tail probability < 1e-4.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

# Default grid truncation and Dixon-Coles low-score correlation.
DEFAULT_K = 10
# Classic Dixon-Coles rho is small & negative: it lifts 0-0 / 1-1 and trims
# 1-0 / 0-1, correcting independent-Poisson's under-count of low draws.
DEFAULT_RHO = -0.13

_FACT = [math.factorial(i) for i in range(DEFAULT_K + 2)]


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _pois(k: int, lam: float) -> float:
    return math.exp(-lam) * lam**k / _FACT[k]


# --------------------------------------------------------------------------
# 1. de-vig
# --------------------------------------------------------------------------
def devig(quotes: List[float], method: str = "proportional") -> List[float]:
    """Normalize a set of (overround) prices into fair probabilities summing to 1."""
    vals = [max(0.0, float(q)) for q in quotes]
    total = sum(vals)
    if total <= 0:
        n = len(vals)
        return [1.0 / n] * n if n else []
    if method == "power":
        # Power de-vig: find exponent so Σ q^a = 1 (favourite-longshot aware).
        a = _solve_power_exponent(vals)
        powered = [v**a for v in vals]
        s = sum(powered)
        return [p / s for p in powered]
    return [v / total for v in vals]  # proportional (baseline)


def _solve_power_exponent(vals: List[float]) -> float:
    lo, hi = 0.01, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        s = sum(v**mid for v in vals)
        if s > 1.0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# --------------------------------------------------------------------------
# 2. goal model — score matrix and its 1X2 aggregation
# --------------------------------------------------------------------------
def _tau(i: int, j: int, lam_h: float, lam_a: float, rho: float) -> float:
    """Dixon-Coles low-score correction (only the 2x2 low corner is altered)."""
    if i == 0 and j == 0:
        return 1.0 - lam_h * lam_a * rho
    if i == 0 and j == 1:
        return 1.0 + lam_h * rho
    if i == 1 and j == 0:
        return 1.0 + lam_a * rho
    if i == 1 and j == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(
    lam_h: float,
    lam_a: float,
    k: int = DEFAULT_K,
    model: str = "dixon_coles",
    rho: float = DEFAULT_RHO,
) -> Dict[Tuple[int, int], float]:
    """Generate a normalized (i,j) -> probability matrix over i,j in [0,k]."""
    use_dc = model == "dixon_coles"
    ph = [_pois(i, lam_h) for i in range(k + 1)]
    pa = [_pois(j, lam_a) for j in range(k + 1)]
    matrix: Dict[Tuple[int, int], float] = {}
    total = 0.0
    for i in range(k + 1):
        for j in range(k + 1):
            p = ph[i] * pa[j]
            if use_dc and i <= 1 and j <= 1:
                p *= max(0.0, _tau(i, j, lam_h, lam_a, rho))
            matrix[(i, j)] = p
            total += p
    if total > 0:
        for key in matrix:
            matrix[key] /= total
    return matrix


def matrix_1x2(matrix: Dict[Tuple[int, int], float]) -> Tuple[float, float, float]:
    """Aggregate a score matrix back to (P_home, P_draw, P_away)."""
    ph = pd = pa = 0.0
    for (i, j), p in matrix.items():
        if i > j:
            ph += p
        elif i == j:
            pd += p
        else:
            pa += p
    return ph, pd, pa


# --------------------------------------------------------------------------
# 3. fit (λH, λA) so the model's 1X2 matches the fair 1X2
# --------------------------------------------------------------------------
def fit_lambdas(
    p_home: float,
    p_away: float,
    model: str = "dixon_coles",
    rho: float = DEFAULT_RHO,
    k: int = DEFAULT_K,
) -> Tuple[float, float]:
    """Solve (λH, λA) such that the model's normalized 1X2 matches targets."""
    p_home = _clamp(p_home, 1e-4, 1 - 1e-4)
    p_away = _clamp(p_away, 1e-4, 1 - 1e-4)

    def resid(lh: float, la: float) -> Tuple[float, float]:
        ph, _pd, pa = matrix_1x2(score_matrix(lh, la, k, model, rho))
        return ph - p_home, pa - p_away

    # Initial guess: total-goals prior ~2.6, split by 1X2 lean.
    mu = 2.6
    lean = _clamp(0.5 + (p_home - p_away), 0.1, 0.9)
    lam_h = _clamp(mu * lean, 0.05, 8.0)
    lam_a = _clamp(mu * (1 - lean), 0.05, 8.0)

    eps = 1e-5
    for _ in range(80):
        f1, f2 = resid(lam_h, lam_a)
        if abs(f1) < 1e-9 and abs(f2) < 1e-9:
            break
        f1h, f2h = resid(lam_h + eps, lam_a)
        f1a, f2a = resid(lam_h, lam_a + eps)
        j11 = (f1h - f1) / eps
        j21 = (f2h - f2) / eps
        j12 = (f1a - f1) / eps
        j22 = (f2a - f2) / eps
        det = j11 * j22 - j12 * j21
        if abs(det) < 1e-12:
            break
        d_h = (f1 * j22 - f2 * j12) / det
        d_a = (j11 * f2 - j21 * f1) / det
        lam_h = _clamp(lam_h - d_h, 0.02, 10.0)
        lam_a = _clamp(lam_a - d_a, 0.02, 10.0)

    # Fallback: coarse grid + refine if Newton didn't converge.
    f1, f2 = resid(lam_h, lam_a)
    if abs(f1) > 1e-3 or abs(f2) > 1e-3:
        lam_h, lam_a = _grid_fit(resid)
    return lam_h, lam_a


def _grid_fit(resid) -> Tuple[float, float]:
    best = (1.3, 1.0)
    best_err = float("inf")
    grid = [0.05 + 0.1 * n for n in range(80)]  # 0.05 .. ~7.95
    for lh in grid:
        for la in grid:
            f1, f2 = resid(lh, la)
            err = f1 * f1 + f2 * f2
            if err < best_err:
                best_err = err
                best = (lh, la)
    return best


# --------------------------------------------------------------------------
# 4. bucketize to Polymarket's labels (16 scores + "Other")
# --------------------------------------------------------------------------
def parse_score_label(label: str) -> Optional[Tuple[int, int]]:
    """'1 - 0' -> (1, 0); 'Other'/'Any Other Score' -> None."""
    import re

    m = re.search(r"(\d+)\s*-\s*(\d+)", label)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def bucketize(
    matrix: Dict[Tuple[int, int], float], score_labels: List[str]
) -> Dict[str, float]:
    """Aggregate the full matrix into the given labels; remainder -> 'Other' label."""
    explicit: Dict[Tuple[int, int], str] = {}
    other_label = "Other"
    for lbl in score_labels:
        ij = parse_score_label(lbl)
        if ij is None:
            other_label = lbl  # keep whatever the book calls the catch-all
        else:
            explicit[ij] = lbl

    buckets: Dict[str, float] = {lbl: 0.0 for lbl in score_labels}
    for (i, j), p in matrix.items():
        lbl = explicit.get((i, j))
        if lbl is not None:
            buckets[lbl] += p
        else:
            buckets[other_label] = buckets.get(other_label, 0.0) + p
    return buckets


# --------------------------------------------------------------------------
# 5. compare + value edges
# --------------------------------------------------------------------------
def _spread_fields(price: float, bid) -> dict:
    """Spread of the bought token: ask(=price) minus its own bid."""
    if bid is None or bid <= 0 or price <= 0:
        return {"bid": None, "spread": None, "spread_pct": None}
    spread = max(0.0, price - bid)
    return {"bid": round(bid, 4), "spread": round(spread, 4), "spread_pct": round(spread / price, 4)}


def compare(
    model_buckets: Dict[str, float],
    market_buckets: Dict[str, float],
    yes_asks: Dict[str, float],
    yes_bids: Dict[str, float],
    no_asks: Dict[str, float],
    no_bids: Dict[str, float],
    yes_ask_sizes: Dict[str, float],
    no_ask_sizes: Dict[str, float],
    threshold: float = 0.02,
) -> Tuple[Dict[str, dict], List[dict]]:
    """
    Return:
      matrix: label -> {model, market, diff}
      value_edges: sorted list. Two actionable, fresh-money directions per score:
        - buy_yes (该比分会发生): pay ask(YES), fair = model_p,    edge = model_p - ask(YES)
        - buy_no  (该比分不发生): pay ask(NO),  fair = 1 - model_p, edge = (1-model_p) - ask(NO)
      "买入 NO" 与 "卖出 YES" 收益等价（YES+NO=1）。优先用真实 NO 盘口的 ask(NO)；
      若 NO 盘口缺失，则回退用 1 - bid(YES) 作代理价并标记 no_book=False。
    Execution prices are raw (NOT de-vigged) per SCORE_MATRIX.md §6.2.
    """
    matrix: Dict[str, dict] = {}
    edges: List[dict] = []
    for lbl, model_p in model_buckets.items():
        market_p = market_buckets.get(lbl, 0.0)
        matrix[lbl] = {
            "model": round(model_p, 4),
            "market": round(market_p, 4),
            "diff": round(model_p - market_p, 4),
        }
        # --- buy YES (bet it happens) ---
        ay = yes_asks.get(lbl)
        if ay is not None and ay > 0:
            e_buy = model_p - ay
            if e_buy > threshold:
                edges.append(
                    {
                        "label": lbl,
                        "side": "buy_yes",
                        "fair": round(model_p, 4),
                        "price": round(ay, 4),
                        "edge": round(e_buy, 4),
                        "size": round(yes_ask_sizes.get(lbl, 0.0), 2),
                        "no_book": True,
                        **_spread_fields(ay, yes_bids.get(lbl)),
                    }
                )
        # --- buy NO (= sell YES; bet it does NOT happen) ---
        an = no_asks.get(lbl)
        no_book = an is not None and an > 0
        size_no = no_ask_sizes.get(lbl, 0.0)
        if not no_book:
            by = yes_bids.get(lbl)  # proxy: ask(NO) ≈ 1 - bid(YES)
            an = (1.0 - by) if (by is not None and by > 0) else None
            size_no = 0.0
        if an is not None and an > 0:
            fair_no = 1.0 - model_p
            e_no = fair_no - an
            if e_no > threshold:
                edges.append(
                    {
                        "label": lbl,
                        "side": "buy_no",
                        "fair": round(fair_no, 4),
                        "price": round(an, 4),
                        "edge": round(e_no, 4),
                        "size": round(size_no, 2),
                        "no_book": bool(no_book),
                        **_spread_fields(an, no_bids.get(lbl) if no_book else None),
                    }
                )
    edges.sort(key=lambda e: e["edge"], reverse=True)
    return matrix, edges


# --------------------------------------------------------------------------
# top-level convenience
# --------------------------------------------------------------------------
def build_score_model(
    onex2_mid: Tuple[float, float, float],          # (home, draw, away) mids
    score_quotes: List[dict],                        # [{label, bid, ask, bid_size, ask_size}]
    model: str = "dixon_coles",
    rho: float = DEFAULT_RHO,
    devig_method: str = "proportional",
    threshold: float = 0.02,
    k: int = DEFAULT_K,
) -> Optional[dict]:
    """Full pipeline for one game. Returns None if inputs are insufficient."""
    q_h, q_d, q_a = onex2_mid
    if min(q_h, q_d, q_a) <= 0 or not score_quotes:
        return None

    p_home, _p_draw, p_away = devig([q_h, q_d, q_a], devig_method)
    lam_h, lam_a = fit_lambdas(p_home, p_away, model, rho, k)
    matrix = score_matrix(lam_h, lam_a, k, model, rho)

    labels = [q["label"] for q in score_quotes]
    model_buckets = bucketize(matrix, labels)

    # Market buckets from de-vigged mids.
    mids = []
    for q in score_quotes:
        bid, ask = q.get("bid") or 0.0, q.get("ask") or 0.0
        mids.append((bid + ask) / 2 if (bid or ask) else 0.0)
    market_probs = devig(mids, devig_method)
    market_buckets = {labels[i]: market_probs[i] for i in range(len(labels))}
    market_overround = round(sum(mids), 4)

    yes_asks = {q["label"]: q.get("ask") for q in score_quotes}
    yes_bids = {q["label"]: q.get("bid") for q in score_quotes}
    no_asks = {q["label"]: q.get("no_ask") for q in score_quotes}
    no_bids = {q["label"]: q.get("no_bid") for q in score_quotes}
    yes_ask_sizes = {q["label"]: q.get("ask_size", 0.0) for q in score_quotes}
    no_ask_sizes = {q["label"]: q.get("no_ask_size", 0.0) for q in score_quotes}

    matrix_cmp, value_edges = compare(
        model_buckets,
        market_buckets,
        yes_asks,
        yes_bids,
        no_asks,
        no_bids,
        yes_ask_sizes,
        no_ask_sizes,
        threshold,
    )

    # Consistency checks (§6.3).
    rec_h, rec_d, rec_a = matrix_1x2(matrix)
    reconstruct_ok = (
        abs(rec_h - p_home) < 5e-3
        and abs(rec_a - p_away) < 5e-3
    )

    return {
        "model": model,
        "rho": rho if model == "dixon_coles" else None,
        "devig": devig_method,
        "lambda_home": round(lam_h, 3),
        "lambda_away": round(lam_a, 3),
        "matrix": matrix_cmp,
        "value_edges": value_edges,
        "checks": {
            "model_sum": round(sum(model_buckets.values()), 4),
            "market_sum": round(sum(market_buckets.values()), 4),
            "market_overround": market_overround,
            "onex2_reconstruct_ok": reconstruct_ok,
        },
    }
