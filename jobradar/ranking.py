"""Re-rank stored jobs at query time, without rescanning or rescoring.

Every score component is persisted in `score_breakdown`, so the dashboard can
recompute a ranking under different priorities instantly. Changing mode is a
sort, not a scan: it never touches the network and never rewrites the database.

The stored `score` column remains the balanced score. Modes only affect display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    hint: str
    #: Multipliers applied to each stored component before recombining.
    weights: dict[str, float] = field(default_factory=dict)
    #: Pulls every role weight toward 100. 0 keeps the role hierarchy intact,
    #: 1 flattens it completely. Lets a software role at a top payer compete
    #: with a quant role at a low one when you are ranking by pay.
    role_flatten: float = 0.0


MODES: dict[str, Mode] = {
    "balanced": Mode(
        "balanced", "Best fit",
        "Default. Role relevance first, then seniority, location and freshness.",
    ),
    "junior": Mode(
        "junior", "Most winnable",
        "Pushes graduate, campus and no-experience-stated roles to the top.",
        weights={"seniority": 2.4, "years": 2.2},
    ),
    "pay": Mode(
        "pay", "Best paid",
        "Ranks by the market-specific pay bands in your profile. Flattens role "
        "weight so compensation can influence the order across role families.",
        weights={"pay": 2.4, "company": 1.4},
        role_flatten=0.45,
    ),
    "local": Mode(
        "local", "Closest to home",
        "Weights your commutable radius far above anywhere you would relocate to.",
        weights={"location": 2.2},
    ),
    "fresh": Mode(
        "fresh", "Newest",
        "Prioritises just-posted roles, where applying early actually matters.",
        weights={"recency": 3.5},
    ),
}

DEFAULT_MODE = "balanced"

#: breakdown key -> the weight name that scales it.
_COMPONENTS = {
    "seniority_pts": "seniority",
    "years_pts": "years",
    "location_pts": "location",
    "recency_pts": "recency",
    "language_pts": "language",
    "company_pts": "company",
    "skill_pts": "skill",
    "pay_pts": "pay",
}

#: pay_pts is recorded during scoring but deliberately left out of the balanced
#: total, because company tier already rewards the same employers. It only
#: enters the ranking when a mode gives it a weight.
_OFF_BY_DEFAULT = {"pay"}


def effective_score(breakdown: dict[str, Any], mode: Mode,
                    overrides: dict[str, float] | None = None) -> float:
    """Recompute a job's score under `mode`. Falls back safely on old rows."""
    if not breakdown:
        return 0.0

    base = float(breakdown.get("base") or 0.0)
    if base <= 0:
        return 0.0

    weights = dict(mode.weights)
    if overrides:
        weights.update(overrides)

    adjustments = float(breakdown.get("penalty_pts") or 0.0)
    for key, name in _COMPONENTS.items():
        value = float(breakdown.get(key) or 0.0)
        default = 0.0 if name in _OFF_BY_DEFAULT else 1.0
        adjustments += value * weights.get(name, default)

    flatten = max(0.0, min(1.0, mode.role_flatten))
    effective_base = base + (100.0 - base) * flatten

    score = effective_base * max(0.05, 1.0 + adjustments / 100.0)

    # Hard caps are non-negotiable in every mode: no weighting should be able
    # to lift a senior or PhD-gated role back to the top of the list.
    ceiling = breakdown.get("cap_ceiling")
    if ceiling is not None:
        score = min(score, float(ceiling))

    return round(max(score, 0.0), 1)


def rank(rows: list[dict], mode_key: str,
         overrides: dict[str, float] | None = None) -> list[dict]:
    """Attach `score` under the chosen mode and sort. Mutates the dicts."""
    mode = MODES.get(mode_key or DEFAULT_MODE, MODES[DEFAULT_MODE])

    if mode.key == DEFAULT_MODE and not overrides:
        # Stored score already is the balanced score; keep the DB's ordering.
        for row in rows:
            row["ranked_score"] = row.get("score", 0.0)
        return rows

    for row in rows:
        row["ranked_score"] = effective_score(row.get("score_breakdown") or {},
                                              mode, overrides)
    rows.sort(key=lambda r: (-r["ranked_score"], r.get("first_seen") or ""))
    return rows


def mode_list() -> list[dict[str, str]]:
    return [{"key": m.key, "label": m.label, "hint": m.hint} for m in MODES.values()]
