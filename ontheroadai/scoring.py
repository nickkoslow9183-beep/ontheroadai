"""Transparent 0-100 content opportunity score.

Every idea gets six 0-10 inputs from a human (later: from the Trend Scout and
Analytics agents) plus one input derived from footage rights. Each factor is
worth a fixed number of points; the total is simply the sum. The breakdown is
stored with the idea so anyone can see exactly why it scored what it did.
"""

from __future__ import annotations

from datetime import date

WEIGHTS = {
    "trend_momentum": 20,
    "audience_fit": 15,
    "hook_strength": 20,
    "conversion_potential": 15,
    "timeliness": 10,
    "production_ease": 10,
    "rights_clarity": 10,
}
assert sum(WEIGHTS.values()) == 100

FACTORS = {
    "trend_momentum": ("Trend momentum", "Is the topic rising right now?"),
    "audience_fit": ("Audience fit", "How well it matches this brand's audience."),
    "hook_strength": ("Hook strength", "Will the first 1-2 seconds stop the scroll?"),
    "conversion_potential": ("Follower conversion", "Does it give viewers a reason to follow?"),
    "timeliness": ("Timeliness", "Does it need to go out soon to matter?"),
    "production_ease": ("Production ease", "10 = quick to make, 0 = very heavy production."),
    "rights_clarity": ("Rights clarity", "Derived from linked footage permissions (not editable)."),
}


def effective_source_status(source, today: date | None = None) -> str:
    """A 'granted' permission whose expiry date has passed counts as expired."""
    status = source["status"]
    expires = source["expires_on"] if "expires_on" in source.keys() else None
    if status == "granted" and expires:
        today = today or date.today()
        try:
            if date.fromisoformat(expires) < today:
                return "expired"
        except ValueError:
            pass
    return status


def rights_clarity(needs_external_footage: bool, sources, today: date | None = None) -> tuple[int, str]:
    """Return (0-10 value, explanation) for the rights factor."""
    if not needs_external_footage:
        return 10, "Uses only our own footage; no third-party rights needed."
    if not sources:
        return 0, "Needs external footage but no source is linked yet."
    statuses = [effective_source_status(s, today) for s in sources]
    if any(s in ("denied", "expired") for s in statuses):
        return 0, "At least one linked source is denied or expired."
    if all(s == "granted" for s in statuses):
        return 10, "All linked sources have documented permission."
    if any(s == "unknown" for s in statuses):
        return 2, "At least one linked source has unknown rights status."
    return 5, "Permission requested but not yet granted for every source."


def compute_score(inputs: dict, needs_external_footage: bool, sources, today: date | None = None) -> dict:
    """Compute the score and a full, human-readable breakdown."""
    rights_value, rights_reason = rights_clarity(needs_external_footage, sources, today)
    values = {k: int(inputs[k]) for k in WEIGHTS if k != "rights_clarity"}
    values["rights_clarity"] = rights_value

    factors = []
    total = 0.0
    for key, weight in WEIGHTS.items():
        value = values[key]
        if not 0 <= value <= 10:
            raise ValueError(f"{key} must be between 0 and 10, got {value}")
        points = value / 10 * weight
        total += points
        label, explanation = FACTORS[key]
        factors.append(
            {
                "key": key,
                "label": label,
                "input": value,
                "weight": weight,
                "points": round(points, 1),
                "explanation": rights_reason if key == "rights_clarity" else explanation,
            }
        )
    return {
        "total": int(round(total)),
        "factors": factors,
        "rights_hold": needs_external_footage and rights_value < 10,
        "formula": "score = sum(input / 10 x weight) over all factors; weights sum to 100",
    }
