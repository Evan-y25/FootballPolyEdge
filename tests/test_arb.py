"""Integration test: ArbExecutor detects a 1X2 back-arb, opens an equal-share
basket, locks the edge, and settles to the locked profit."""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.paper import PaperTrader  # noqa: E402
from app.arb_executor import ArbExecutor  # noqa: E402


def _O(label, yes, no):
    return types.SimpleNamespace(label=label, yes_token=yes, no_token=no,
                                 init_yes=0.0, init_no=0.0)


def _game():
    return types.SimpleNamespace(
        slug="g1", home="A", away="B",
        home_win=_O("home", "Hy", "Hn"), draw=_O("draw", "Dy", "Dn"),
        away_win=_O("away", "Ay", "An"))


class FakeState:
    def __init__(self, book):
        self.book = book
        self.games = [_game()]

    def token_best(self, t):
        return self.book.get(t, {"bid": 0, "ask": 0, "bid_size": 0, "ask_size": 0, "live": False})

    def find_game(self, slug):
        return self.games[0] if slug == "g1" else None

    def find_outcome(self, game, market, label):
        return {"home": game.home_win, "draw": game.draw, "away": game.away_win}[label]


def test_back_arb_locks_and_settles(tmp_path):
    # back-arb: yes asks 0.30+0.30+0.30 = 0.90 < 1 -> edge 0.10
    book = {t: {"bid": 0.29, "ask": 0.30, "bid_size": 1000, "ask_size": 1000, "live": True}
            for t in ("Hy", "Dy", "Ay")}
    # NO side priced so NO lay-arb does NOT trigger (sum no_ask ~ 2.1 > 2)
    for t in ("Hn", "Dn", "An"):
        book[t] = {"bid": 0.69, "ask": 0.70, "bid_size": 1000, "ask_size": 1000, "live": True}
    st = FakeState(book)
    paper = PaperTrader(st, tmp_path / "arb.json", 2000.0)
    arb = ArbExecutor(st, paper)

    res = arb.scan_once()
    assert res["opened"] == 1, res
    b = arb.baskets[0]
    assert b["kind"] == "back"
    assert abs(b["edge"] - 0.10) < 1e-6, b
    # capped by ARB_MAX_STAKE -> sets*0.90 <= max_stake; profit = sets*0.10
    assert abs(b["profit"] - b["sets"] * 0.10) < 0.02
    assert len(b["legs"]) == 3
    # second scan should NOT duplicate (basket still open)
    assert arb.scan_once()["opened"] == 0

    # settle: home happens -> Hy wins ($1), Dy/Ay -> $0. realized == locked profit.
    paper.settle("g1", {("1x2", "home"): "yes", ("1x2", "draw"): "no", ("1x2", "away"): "no"})
    snap = paper.snapshot()
    assert abs(snap["realized_pnl"] - b["profit"]) < 0.05, (snap["realized_pnl"], b["profit"])


def test_no_arb_when_overround(tmp_path):
    # normal overround: yes asks sum 1.05 (>1, no back), no asks sum 2.05 (>2, no lay)
    book = {}
    for t in ("Hy", "Dy", "Ay"):
        book[t] = {"bid": 0.34, "ask": 0.35, "bid_size": 1000, "ask_size": 1000, "live": True}
    for t in ("Hn", "Dn", "An"):
        book[t] = {"bid": 0.67, "ask": 0.683, "bid_size": 1000, "ask_size": 1000, "live": True}
    arb = ArbExecutor(FakeState(book), PaperTrader(FakeState(book), tmp_path / "a.json", 2000.0))
    assert arb.scan_once()["opened"] == 0


if __name__ == "__main__":
    import tempfile, pathlib
    passed = 0
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in fns:
        try:
            fn(pathlib.Path(tempfile.mkdtemp()))
            print(f"PASS {name}"); passed += 1
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc(); print(f"FAIL {name}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
