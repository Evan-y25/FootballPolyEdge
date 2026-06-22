"""
Strategy genome — the single source of truth for the strategy's behavior.

Encodes BOTH numeric parameters and logic switches, so the (non-LLM) evolution
loop can search over strategy *logic* by flipping pre-coded branches, not just
tuning numbers. The genome is a plain JSON file that gets committed to git on
every adopted evolution — that IS the "code change" pushed to the remote.

Kinds:
  frac  fraction 0..1            int   integer
  num   positive number ($)      bool  true/false
  enum  one of a fixed list
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Dict

logger = logging.getLogger(__name__)

# key -> (kind, *bounds_or_choices)
GENOME_SPEC: Dict[str, tuple] = {
    # --- numeric params ---
    "bankroll": ("num", 1.0, 1_000_000.0),
    "edge_threshold": ("frac", 0.0, 0.5),
    "min_price": ("frac", 0.0, 1.0),
    "take_profit": ("frac", 0.0, 5.0),
    "stop_loss": ("frac", 0.0, 1.0),
    "max_positions": ("int", 1, 200),
    "max_per_game": ("int", 1, 50),
    "max_exposure": ("frac", 0.0, 1.0),
    "force_close_min": ("int", 0, 240),
    "hold_to_settle_price": ("frac", 0.0, 1.0),
    "max_spread": ("frac", 0.0, 1.0),
    "add_drop": ("frac", 0.0, 1.0),
    "reentry_cooldown": ("int", 0, 86400),
    # --- logic switches (the "code logic" the evolution can flip) ---
    "stop_loss_enabled": ("bool",),
    "addon_enabled": ("bool",),
    "addon_pre_match_only": ("bool",),   # don't average down on live games
    "direction": ("enum", ["both", "no_only", "yes_only"]),
}

DEFAULT_GENOME: Dict[str, Any] = {
    "bankroll": 100.0,
    "edge_threshold": 0.03,
    "min_price": 0.50,
    "take_profit": 0.10,
    "stop_loss": 0.30,
    "max_positions": 20,
    "max_per_game": 3,
    "max_exposure": 0.95,
    "force_close_min": 10,
    "hold_to_settle_price": 0.80,
    "max_spread": 0.08,
    "add_drop": 0.10,
    "reentry_cooldown": 300,
    "stop_loss_enabled": True,
    "addon_enabled": True,
    "addon_pre_match_only": True,
    "direction": "both",
}


def coerce(key: str, raw: Any):
    """Validate + clamp one value to its spec. Returns coerced value or None if invalid."""
    spec = GENOME_SPEC.get(key)
    if not spec:
        return None
    kind = spec[0]
    try:
        if kind == "int":
            v = int(round(float(raw)))
            return max(spec[1], min(spec[2], v))
        if kind in ("frac", "num"):
            v = float(raw)
            return max(spec[1], min(spec[2], v))
        if kind == "bool":
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on")
            return bool(raw)
        if kind == "enum":
            return raw if raw in spec[1] else None
    except (TypeError, ValueError):
        return None
    return None


def sanitize(genome: Dict[str, Any]) -> Dict[str, Any]:
    """Return a full, valid genome: defaults overlaid with valid provided values."""
    out = dict(DEFAULT_GENOME)
    for k, v in (genome or {}).items():
        cv = coerce(k, v)
        if cv is not None:
            out[k] = cv
    return out


def load(path: str) -> Dict[str, Any]:
    try:
        data = json.loads(pathlib.Path(path).read_text())
        return sanitize(data)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_GENOME)


def save(path: str, genome: Dict[str, Any]) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sanitize(genome), ensure_ascii=False, indent=2))
