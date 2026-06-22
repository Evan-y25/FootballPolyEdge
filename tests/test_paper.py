"""Unit tests for the paper trader's average-down (add_to) logic."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.paper import PaperTrader  # noqa: E402


class FakeState:
    """Minimal state stub exposing token_best at a fixed ask."""

    def __init__(self, ask: float) -> None:
        self._ask = ask

    def token_best(self, token: str) -> dict:
        return {"bid": self._ask - 0.01, "bid_size": 100, "ask": self._ask,
                "ask_size": 100, "live": True}


def _pos(**kw):
    base = {
        "id": 1, "slug": "g", "home": "A", "away": "B", "market": "score",
        "label": "2 - 0", "side": "no", "token": "tok",
        "entry_price": 0.90, "shares": 10.0, "stake": 9.0,
        "opened_at": int(time.time()), "status": "open", "added": False,
        "close_price": None, "proceeds": None, "realized_pnl": None, "closed_at": None,
    }
    base.update(kw)
    return base


def test_add_to_averages_cost_and_increases_shares(tmp_path):
    pt = PaperTrader(FakeState(0.80), tmp_path / "p.json", 100.0)
    pt.positions.append(_pos())   # 10 shares @ 0.90, stake 9.0
    pt._seq = 1
    res = pt.add_to(1, 8.0)        # buy 8/0.80 = 10 more shares
    assert res["ok"], res
    pos = res["position"]
    assert abs(pos["shares"] - 20.0) < 1e-6
    assert abs(pos["stake"] - 17.0) < 1e-6
    assert abs(pos["entry_price"] - 0.85) < 1e-6   # weighted avg (9+8)/20
    assert pos["added"] is True


def test_add_to_only_once(tmp_path):
    pt = PaperTrader(FakeState(0.80), tmp_path / "p.json", 100.0)
    pt.positions.append(_pos())
    pt._seq = 1
    assert pt.add_to(1, 8.0)["ok"]
    second = pt.add_to(1, 8.0)
    assert not second["ok"] and "一次" in second["error"]


def test_add_to_rejects_when_no_ask(tmp_path):
    pt = PaperTrader(FakeState(0.0), tmp_path / "p.json", 100.0)
    pt.positions.append(_pos())
    pt._seq = 1
    res = pt.add_to(1, 8.0)
    assert not res["ok"]


if __name__ == "__main__":
    import tempfile, pathlib
    passed = 0
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        try:
            fn(pathlib.Path(tempfile.mkdtemp()))
            print(f"PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
