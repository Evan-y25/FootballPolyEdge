"""
Per-match self-evolution (Phase 3+4).

When a match resolves, we:
  1. pull our trades on it + the true settlement outcome,
  2. attribute the P&L (premature stop-loss / true losses / averaging-down losses),
  3. write a review report + append a learning,
  4. propose a *bounded* genome adjustment (heuristic, deterministic — no LLM),
  5. gate it: unit tests must pass,
  6. adopt: write genome.json, log to DB, optionally git commit+push to main.

Safety: conservative single-match nudges (small steps, require a clear signal,
min trade count), every change clamped by the genome spec, fully logged + git
留痕, paper-only. The backtest engine (backtest.py) will later replace the
heuristic gate with "must beat current on history" once enough data exists.
"""

from __future__ import annotations

import json
import logging
import pathlib
import subprocess
import time
from typing import Dict, List, Optional

from . import config, genome

logger = logging.getLogger(__name__)

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
MEMORY_DIR = REPO_DIR / "memory"
LEARNINGS = MEMORY_DIR / "learnings.jsonl"
ACTIVE = MEMORY_DIR / "active_learnings.md"
REPORTS_DIR = MEMORY_DIR / "reports"

MIN_TRADES = 3          # need at least this many closed trades to change anything
MAX_CHANGES = 2         # at most this many params changed per match


class Evolver:
    def __init__(self, store, auto, autocommit: bool = False) -> None:
        self.store = store
        self.auto = auto
        self.autocommit = autocommit
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        self._done: set = set()  # slugs already evolved on (this process)

    # ---- analysis -------------------------------------------------------
    def analyze(self, slug: str) -> dict:
        trades = [t for t in self.store.trades_for_slug(slug) if t["status"] == "closed"]
        winners = self.store.resolution_map(slug)  # (market,label) -> 'yes'/'no'
        pnl = sum(t["realized_pnl"] or 0.0 for t in trades)

        premature_sl, true_losses, addon_losses, wins = [], [], [], []
        for t in trades:
            w = winners.get((t["market"], t["label"]))
            won = (w is not None and t["side"] == w)
            if t["close_reason"] == "stop-loss" and won:
                premature_sl.append(t)         # we cut a position that ended up winning
            if (t["realized_pnl"] or 0) < 0 and t["close_reason"] == "settled" and not won:
                true_losses.append(t)          # rode to settlement, score happened
            if t.get("added") and (t["realized_pnl"] or 0) < 0:
                addon_losses.append(t)
            if (t["realized_pnl"] or 0) > 0:
                wins.append(t)

        return {
            "slug": slug,
            "n_trades": len(trades),
            "pnl": round(pnl, 2),
            "wins": len(wins),
            "premature_sl": len(premature_sl),
            "true_losses": len(true_losses),
            "addon_losses": len(addon_losses),
            "home": trades[0]["home"] if trades else "",
            "away": trades[0]["away"] if trades else "",
        }

    def propose(self, a: dict) -> tuple:
        """Return (changes:dict, rationale:list). Bounded, conservative, signal-gated."""
        changes: Dict = {}
        why: List[str] = []
        g = self.auto.params
        if a["n_trades"] < MIN_TRADES:
            return {}, [f"样本不足({a['n_trades']}<{MIN_TRADES})，仅记录不调参"]

        # 1) cutting winners with stop-loss -> loosen stop-loss (toward holding)
        if a["premature_sl"] >= 2:
            new = genome.coerce("stop_loss", g["stop_loss"] + 0.05)
            if new and new != g["stop_loss"]:
                changes["stop_loss"] = new
                why.append(f"{a['premature_sl']} 笔止损砍掉了最终会赢的单 → 放宽止损 {g['stop_loss']:.2f}→{new:.2f}")

        # 2) averaging-down losses -> stop averaging into live drops
        if a["addon_losses"] >= 2 and g.get("addon_pre_match_only") is not True:
            changes["addon_pre_match_only"] = True
            why.append(f"{a['addon_losses']} 笔补仓亏损 → 补仓仅限赛前")

        # 3) too many true losses (real hits) -> be pickier
        if len(changes) < MAX_CHANGES and a["true_losses"] >= 2 and a["pnl"] < 0:
            new = genome.coerce("edge_threshold", g["edge_threshold"] + 0.01)
            if new and new != g["edge_threshold"]:
                changes["edge_threshold"] = new
                why.append(f"{a['true_losses']} 笔冷门命中真亏 + 当场净亏 → 提高价值阈值 {g['edge_threshold']:.2f}→{new:.2f}（更挑剔）")

        if not changes:
            why.append("本场无明确可优化信号，维持现状")
        return dict(list(changes.items())[:MAX_CHANGES]), why

    # ---- gate + adopt ---------------------------------------------------
    def _tests_pass(self) -> bool:
        for tf in ("tests/test_score_model.py", "tests/test_paper.py"):
            try:
                r = subprocess.run(["python3", tf], cwd=str(REPO_DIR),
                                    capture_output=True, text=True, timeout=120)
            except Exception as exc:  # noqa: BLE001
                logger.warning("test run failed to launch: %s", exc)
                return False
            if r.returncode != 0:
                logger.warning("gate: %s failed\n%s", tf, r.stdout[-500:])
                return False
        return True

    def _write_report(self, a: dict, why: List[str], changes: dict, adopted: bool) -> pathlib.Path:
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        path = REPORTS_DIR / f"{ts}-{a['slug']}.md"
        lines = [
            f"# 复盘 {a['home']} vs {a['away']} ({a['slug']})",
            "",
            f"- 交易笔数: {a['n_trades']}  盈利笔: {a['wins']}  当场净盈亏: **{a['pnl']:+.2f}**",
            f"- 亏损归因: 过早止损(砍了赢单) {a['premature_sl']} · 冷门命中真亏 {a['true_losses']} · 补仓亏损 {a['addon_losses']}",
            "",
            "## 优化判断",
            *[f"- {w}" for w in why],
            "",
            f"## 决定: {'已采纳并提交' if adopted else '不调参'}",
            f"- 改动: `{json.dumps(changes, ensure_ascii=False)}`" if changes else "- 改动: 无",
            "",
        ]
        path.write_text("\n".join(lines))
        return path

    def _append_learning(self, a: dict, why: List[str], changes: dict, adopted: bool) -> None:
        rec = {"ts": int(time.time()), "slug": a["slug"], "pnl": a["pnl"],
               "premature_sl": a["premature_sl"], "true_losses": a["true_losses"],
               "addon_losses": a["addon_losses"], "changes": changes, "adopted": adopted,
               "why": why}
        with open(LEARNINGS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._rebuild_active()

    def _rebuild_active(self) -> None:
        try:
            recs = [json.loads(l) for l in LEARNINGS.read_text().splitlines() if l.strip()]
        except FileNotFoundError:
            recs = []
        recent = recs[-12:]
        total = sum(r["pnl"] for r in recs)
        lines = ["# Active Learnings (策略进化记忆)", "",
                 f"累计复盘 {len(recs)} 场 · 累计模拟盈亏 {total:+.2f}", "", "## 最近"]
        for r in reversed(recent):
            t = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
            tag = "✅采纳 " + json.dumps(r["changes"], ensure_ascii=False) if r["adopted"] else "—"
            lines.append(f"- {t} `{r['slug']}` 盈亏{r['pnl']:+.2f} {tag}")
        ACTIVE.write_text("\n".join(lines) + "\n")

    def _git_commit(self, a: dict, changes: dict) -> bool:
        msg = (f"evolve: {a['slug']} pnl={a['pnl']:+.2f} -> {json.dumps(changes, ensure_ascii=False)}\n\n"
               "Auto-evolved from per-match review (paper). "
               "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>")
        try:
            subprocess.run(["git", "add", "genome.json", "memory"], cwd=str(REPO_DIR),
                           check=True, capture_output=True, text=True, timeout=30)
            r = subprocess.run(["git", "commit", "-m", msg], cwd=str(REPO_DIR),
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
                logger.warning("git commit failed: %s", r.stderr[-300:])
                return False
            # Rebase onto remote first to avoid races with other pushers.
            subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=str(REPO_DIR),
                           capture_output=True, text=True, timeout=60)
            push = subprocess.run(["git", "push"], cwd=str(REPO_DIR),
                                  capture_output=True, text=True, timeout=60)
            if push.returncode != 0:
                logger.warning("git push failed: %s", push.stderr[-300:])
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("git commit/push error: %s", exc)
            return False

    def on_match_resolved(self, slug: str) -> Optional[dict]:
        if slug in self._done:
            return None
        self._done.add(slug)
        a = self.analyze(slug)
        changes, why = self.propose(a)

        adopted = False
        if changes:
            # apply to live genome (also persists genome.json), then gate on tests.
            before = dict(self.auto.params)
            self.auto.set_params(changes)
            if self._tests_pass():
                adopted = True
            else:
                self.auto.set_params({k: before[k] for k in changes})  # revert
                why.append("⚠️ 单测未通过，已回滚改动")
                changes = {}

        report = self._write_report(a, why, changes, adopted)
        self._append_learning(a, why, changes, adopted)
        self.store.record_evolution(int(time.time()), len(self._done),
                                    dict(self.auto.params), a["pnl"], a["pnl"],
                                    adopted, "; ".join(why))
        committed = False
        if adopted and self.autocommit:
            committed = self._git_commit(a, changes)

        logger.info("EVOLVE %s pnl=%+.2f changes=%s adopted=%s committed=%s",
                    slug, a["pnl"], changes, adopted, committed)
        self.auto._note("system", f"进化复盘 {slug}: 盈亏{a['pnl']:+.2f} "
                        + (f"采纳 {json.dumps(changes, ensure_ascii=False)}" if adopted else "未调参"))
        return {"analysis": a, "changes": changes, "why": why, "adopted": adopted,
                "committed": committed, "report": str(report)}
