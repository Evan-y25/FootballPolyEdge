"""Unit tests for the score-matrix model (SCORE_MATRIX.md §8.1)."""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import score_model as sm  # noqa: E402


def approx(a, b, tol=1e-6):
    return abs(a - b) < tol


def test_devig_proportional_sums_to_one():
    probs = sm.devig([0.55, 0.30, 0.25])  # overround 1.10
    assert approx(sum(probs), 1.0)
    # ordering preserved
    assert probs[0] > probs[1] > probs[2]


def test_devig_power_sums_to_one():
    probs = sm.devig([0.55, 0.30, 0.25], method="power")
    assert approx(sum(probs), 1.0, tol=1e-4)


def test_matrix_normalized():
    m = sm.score_matrix(1.6, 1.0, model="poisson")
    assert approx(sum(m.values()), 1.0, tol=1e-9)
    m2 = sm.score_matrix(1.6, 1.0, model="dixon_coles", rho=-0.13)
    assert approx(sum(m2.values()), 1.0, tol=1e-9)


def test_fit_recovers_known_lambdas_poisson():
    # Generate 1X2 from known lambdas, then fit should recover them.
    lam_h, lam_a = 1.7, 0.9
    ph, pd, pa = sm.matrix_1x2(sm.score_matrix(lam_h, lam_a, model="poisson"))
    assert approx(ph + pd + pa, 1.0, tol=1e-9)
    fh, fa = sm.fit_lambdas(ph, pa, model="poisson")
    assert approx(fh, lam_h, tol=1e-2), (fh, lam_h)
    assert approx(fa, lam_a, tol=1e-2), (fa, lam_a)


def test_fit_recovers_known_lambdas_dixon_coles():
    lam_h, lam_a, rho = 1.4, 1.1, -0.13
    ph, pd, pa = sm.matrix_1x2(sm.score_matrix(lam_h, lam_a, model="dixon_coles", rho=rho))
    fh, fa = sm.fit_lambdas(ph, pa, model="dixon_coles", rho=rho)
    assert approx(fh, lam_h, tol=2e-2), (fh, lam_h)
    assert approx(fa, lam_a, tol=2e-2), (fa, lam_a)


def test_onex2_reconstruction():
    # Fit from arbitrary fair 1X2, the matrix must aggregate back to it.
    p_home, p_away = 0.55, 0.20
    fh, fa = sm.fit_lambdas(p_home, p_away, model="dixon_coles")
    rec_h, rec_d, rec_a = sm.matrix_1x2(sm.score_matrix(fh, fa, model="dixon_coles"))
    assert approx(rec_h, p_home, tol=5e-3), (rec_h, p_home)
    assert approx(rec_a, p_away, tol=5e-3), (rec_a, p_away)


def test_bucketize_sums_to_one_and_other():
    m = sm.score_matrix(1.6, 1.2, model="dixon_coles")
    labels = [
        "1 - 0", "2 - 0", "2 - 1", "3 - 0", "3 - 1", "3 - 2",
        "0 - 0", "1 - 1", "2 - 2", "3 - 3",
        "0 - 1", "0 - 2", "1 - 2", "0 - 3", "1 - 3", "2 - 3",
        "Other",
    ]
    buckets = sm.bucketize(m, labels)
    assert approx(sum(buckets.values()), 1.0, tol=1e-9)
    assert buckets["Other"] > 0  # tail beyond 0-3 lands here


def test_dixon_coles_boosts_draws_vs_poisson():
    lam_h, lam_a = 1.3, 1.3
    mp = sm.score_matrix(lam_h, lam_a, model="poisson")
    md = sm.score_matrix(lam_h, lam_a, model="dixon_coles", rho=-0.13)
    assert md[(0, 0)] > mp[(0, 0)]
    assert md[(1, 1)] > mp[(1, 1)]


def test_build_score_model_end_to_end():
    score_quotes = [
        {"label": "1 - 0", "bid": 0.10, "ask": 0.12, "bid_size": 100, "ask_size": 200,
         "no_ask": 0.90, "no_ask_size": 300},
        {"label": "0 - 0", "bid": 0.08, "ask": 0.10, "bid_size": 100, "ask_size": 200,
         "no_ask": 0.91, "no_ask_size": 300},
        {"label": "Other", "bid": 0.04, "ask": 0.06, "bid_size": 100, "ask_size": 200,
         "no_ask": 0.95, "no_ask_size": 300},
    ]
    out = sm.build_score_model((0.55, 0.28, 0.22), score_quotes)
    assert out is not None
    assert out["lambda_home"] > 0 and out["lambda_away"] > 0
    assert out["checks"]["onex2_reconstruct_ok"] is True
    assert set(out["matrix"].keys()) == {"1 - 0", "0 - 0", "Other"}
    for e in out["value_edges"]:
        assert e["side"] in ("buy_yes", "buy_no")


def test_compare_buy_no_uses_real_no_ask_and_proxy():
    model = {"0 - 0": 0.20}
    market = {"0 - 0": 0.10}
    # Real NO ask present: fair(NO)=0.80, ask(NO)=0.70 -> edge 0.10, no_book True
    _mx, edges = sm.compare(
        model, market,
        yes_asks={"0 - 0": 0.50}, yes_bids={"0 - 0": 0.05},
        no_asks={"0 - 0": 0.70}, no_bids={"0 - 0": 0.68},
        yes_ask_sizes={"0 - 0": 0}, no_ask_sizes={"0 - 0": 500},
        threshold=0.02,
    )
    buy_no = [e for e in edges if e["side"] == "buy_no"][0]
    assert approx(buy_no["edge"], 0.10, tol=1e-9)
    assert buy_no["no_book"] is True and buy_no["size"] == 500
    # spread = ask(0.70) - bid(0.68) = 0.02 -> spread_pct ~ 0.0286
    assert approx(buy_no["spread"], 0.02, tol=1e-9)
    assert approx(buy_no["spread_pct"], 0.02 / 0.70, tol=1e-3)

    # No NO ask -> proxy 1 - bid(YES) = 1 - 0.05 = 0.95, fair(NO)=0.80 -> edge -0.15 (no edge)
    _mx2, edges2 = sm.compare(
        model, market,
        yes_asks={"0 - 0": 0.50}, yes_bids={"0 - 0": 0.05},
        no_asks={"0 - 0": 0.0}, no_bids={"0 - 0": 0.0},
        yes_ask_sizes={"0 - 0": 0}, no_ask_sizes={"0 - 0": 0},
        threshold=0.02,
    )
    assert not [e for e in edges2 if e["side"] == "buy_no"]


def test_wide_spread_fields_present():
    # buy_yes with a wide spread: ask 0.43, bid 0.23 -> spread_pct ~ 0.465
    model = {"Other": 0.588}
    market = {"Other": 0.40}
    _mx, edges = sm.compare(
        model, market,
        yes_asks={"Other": 0.43}, yes_bids={"Other": 0.23},
        no_asks={"Other": 0.0}, no_bids={"Other": 0.0},
        yes_ask_sizes={"Other": 100}, no_ask_sizes={"Other": 0},
        threshold=0.02,
    )
    buy_yes = [e for e in edges if e["side"] == "buy_yes"][0]
    assert approx(buy_yes["spread"], 0.20, tol=1e-9)
    assert buy_yes["spread_pct"] > 0.45  # would be filtered by AUTO_MAX_SPREAD=0.08


if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {fn.__name__}: {e}")
    print(f"\n{passed}/{len(funcs)} passed")
