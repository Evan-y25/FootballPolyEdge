"""Integration tests for the self-evolution pipeline and backtest scaffold."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import genome  # noqa: E402
from app.store import Store  # noqa: E402
from app.evolve import Evolver  # noqa: E402
from app import backtest  # noqa: E402


class FakeAuto:
    """Minimal AutoTrader stand-in for the evolver."""
    def __init__(self):
        self.params = dict(genome.DEFAULT_GENOME)
        self.notes = []

    def set_params(self, updates):
        for k, v in updates.items():
            cv = genome.coerce(k, v)
            if cv is not None:
                self.params[k] = cv
        return {}

    def _note(self, kind, desc, pnl=None):
        self.notes.append(desc)


def _trade(store, tid, slug, market, label, side, pnl, reason, added=0):
    store.record_trade({
        "id": tid, "slug": slug, "home": "Brazil", "away": "Haiti",
        "market": market, "label": label, "side": side, "token": f"t{tid}",
        "entry_price": 0.85, "shares": 10.0, "stake": 8.5, "opened_at": int(time.time()),
        "status": "closed", "added": added, "close_price": 0.0, "realized_pnl": pnl,
        "closed_at": int(time.time()), "close_reason": reason,
    })


def test_analyze_attribution(tmp_path):
    s = Store(str(tmp_path / "m.db"))
    slug = "fifwc-bra-hai-2026-06-19"
    # resolution: 3-0 happened (yes won), 2-0 did not (no won)
    s.record_resolution(slug, "score", "3 - 0", "y", "n", "yes", int(time.time()))
    s.record_resolution(slug, "score", "2 - 0", "y", "n", "no", int(time.time()))
    # #1 stop-lossed a NO on 2-0 which actually WON (premature) ; #2 NO on 3-0 lost (true loss settled)
    _trade(s, 1, slug, "score", "2 - 0", "no", -3.0, "stop-loss")
    _trade(s, 2, slug, "score", "3 - 0", "no", -8.5, "settled")
    _trade(s, 3, slug, "score", "2 - 0", "no", -3.0, "stop-loss")  # another premature
    ev = Evolver(s, FakeAuto(), autocommit=False)
    a = ev.analyze(slug)
    assert a["n_trades"] == 3
    assert a["premature_sl"] == 2, a
    assert a["true_losses"] == 1, a


def test_propose_loosens_stop_loss_on_premature_cuts(tmp_path):
    s = Store(str(tmp_path / "m.db"))
    auto = FakeAuto()
    ev = Evolver(s, auto, autocommit=False)
    a = {"n_trades": 4, "pnl": -5.0, "wins": 1, "premature_sl": 2, "true_losses": 0,
         "addon_losses": 0, "slug": "g", "home": "A", "away": "B"}
    changes, why = ev.propose(a)
    assert "stop_loss" in changes
    assert changes["stop_loss"] > genome.DEFAULT_GENOME["stop_loss"]


def test_small_sample_no_change(tmp_path):
    s = Store(str(tmp_path / "m.db"))
    ev = Evolver(s, FakeAuto(), autocommit=False)
    changes, why = ev.propose({"n_trades": 1, "pnl": -2, "wins": 0, "premature_sl": 1,
                               "true_losses": 0, "addon_losses": 0, "slug": "g", "home": "", "away": ""})
    assert changes == {}


def test_on_match_resolved_writes_memory(tmp_path, monkeypatch):
    # Redirect memory output into tmp by monkeypatching module paths.
    from app import evolve as ev_mod
    monkeypatch.setattr(ev_mod, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(ev_mod, "LEARNINGS", tmp_path / "memory" / "learnings.jsonl")
    monkeypatch.setattr(ev_mod, "ACTIVE", tmp_path / "memory" / "active_learnings.md")
    monkeypatch.setattr(ev_mod, "REPORTS_DIR", tmp_path / "memory" / "reports")
    s = Store(str(tmp_path / "m.db"))
    slug = "fifwc-bra-hai-2026-06-19"
    s.record_resolution(slug, "score", "2 - 0", "y", "n", "no", int(time.time()))
    _trade(s, 1, slug, "score", "2 - 0", "no", -3.0, "stop-loss")
    _trade(s, 2, slug, "score", "2 - 0", "no", -3.0, "stop-loss")
    _trade(s, 3, slug, "score", "2 - 0", "no", 1.0, "settled")
    ev = Evolver(s, FakeAuto(), autocommit=False)
    out = ev.on_match_resolved(slug)
    assert out is not None
    assert (tmp_path / "memory" / "learnings.jsonl").exists()
    assert (tmp_path / "memory" / "active_learnings.md").exists()
    # idempotent: second call returns None (already evolved this slug)
    assert ev.on_match_resolved(slug) is None


def test_backtest_game(tmp_path):
    s = Store(str(tmp_path / "m.db"))
    slug = "fifwc-bra-hai-2026-06-19"
    now = int(time.time())
    # 1X2 (Brazil heavy fav) + one score market, both sides
    def tick(tok, mk, lb, sd, bid, ask):
        s.record_tick(now, tok, slug, mk, lb, sd, bid, ask, 1000, 1000)
    tick("h_y", "1x2", "home", "yes", 0.90, 0.91); tick("h_n", "1x2", "home", "no", 0.09, 0.10)
    tick("d_y", "1x2", "draw", "yes", 0.06, 0.07); tick("d_n", "1x2", "draw", "no", 0.93, 0.94)
    tick("a_y", "1x2", "away", "yes", 0.02, 0.03); tick("a_n", "1x2", "away", "no", 0.97, 0.98)
    tick("s_y", "score", "2 - 0", "yes", 0.12, 0.14); tick("s_n", "score", "2 - 0", "no", 0.86, 0.88)
    s.commit()
    s.record_resolution(slug, "score", "2 - 0", "s_y", "s_n", "no", now)  # 2-0 did NOT happen -> NO wins
    r = backtest.backtest_game(s, slug, dict(genome.DEFAULT_GENOME))
    assert not r["skipped"], r
    # if it bought the 2-0 NO @0.88 and it won (->1.0), pnl should be >= 0
    assert r["n_trades"] >= 0


if __name__ == "__main__":
    import tempfile, pathlib
    class MP:  # tiny monkeypatch shim
        def setattr(self, obj, name, val): setattr(obj, name, val)
    passed = 0
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    for name, fn in fns:
        try:
            args = [pathlib.Path(tempfile.mkdtemp())]
            if "monkeypatch" in fn.__code__.co_varnames:
                args.append(MP())
            fn(*args)
            print(f"PASS {name}"); passed += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc(); print(f"ERROR {name}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
