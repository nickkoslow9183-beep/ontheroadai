from datetime import date

import pytest

from ontheroadai.scoring import WEIGHTS, compute_score, rights_clarity

ALL_TEN = {k: 10 for k in WEIGHTS if k != "rights_clarity"}
ALL_ZERO = {k: 0 for k in WEIGHTS if k != "rights_clarity"}


def test_weights_sum_to_100():
    assert sum(WEIGHTS.values()) == 100


def test_max_and_min_scores():
    assert compute_score(ALL_TEN, False, [])["total"] == 100
    # own footage still earns the 10 rights points
    assert compute_score(ALL_ZERO, False, [])["total"] == 10
    assert compute_score(ALL_ZERO, True, [])["total"] == 0


def test_breakdown_points_add_up():
    inputs = {"trend_momentum": 7, "audience_fit": 6, "hook_strength": 9, "conversion_potential": 4,
              "timeliness": 3, "production_ease": 8}
    result = compute_score(inputs, False, [])
    assert round(sum(f["points"] for f in result["factors"])) == result["total"]
    # 14 + 9 + 18 + 6 + 3 + 8 + 10 = 68
    assert result["total"] == 68
    assert {f["key"] for f in result["factors"]} == set(WEIGHTS)


def test_rights_clarity_rules():
    assert rights_clarity(False, [])[0] == 10
    assert rights_clarity(True, [])[0] == 0
    assert rights_clarity(True, [{"status": "granted", "expires_on": None}])[0] == 10
    assert rights_clarity(True, [{"status": "requested", "expires_on": None}])[0] == 5
    assert rights_clarity(True, [{"status": "unknown", "expires_on": None}])[0] == 2
    assert rights_clarity(True, [{"status": "granted", "expires_on": None},
                                 {"status": "denied", "expires_on": None}])[0] == 0


def test_expired_grant_counts_as_expired():
    src = [{"status": "granted", "expires_on": "2026-01-01"}]
    assert rights_clarity(True, src, today=date(2026, 6, 1))[0] == 0
    assert rights_clarity(True, src, today=date(2025, 6, 1))[0] == 10


def test_rights_hold_flag():
    assert compute_score(ALL_TEN, True, [{"status": "requested", "expires_on": None}])["rights_hold"] is True
    assert compute_score(ALL_TEN, False, [])["rights_hold"] is False


def test_out_of_range_input_rejected():
    with pytest.raises(ValueError):
        compute_score({**ALL_TEN, "hook_strength": 11}, False, [])
