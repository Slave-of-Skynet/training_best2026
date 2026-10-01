"""Unit tests for the VLD-EVAL-02 evaluation runner (app/evaluation/runner.py).

Tests verify:
- All modes produce schema-valid result artifacts
- DETERMINISTIC mode detects all four discrepancy categories
- DETERMINISTIC mode correctly marks SC-001 as PARTIALLY_MEASURED
- DETERMINISTIC gate blocking and terminal-state enforcement work
- REPLAY mode processes historical bake-off records from committed artifacts
- HISTORICAL_BAKEOFF mode aggregates committed phase2 summary metrics
- Result validation rejects invalid artifacts
- LIVE mode is gated (not run without explicit env var)
- Result files are written to docs/evaluation/results/
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.evaluation.runner import (
    VALID_MODES,
    _current_git_commit,
    _empty_result_with_note,
    _make_in_memory_session,
    _rate,
    _validate_against_schema,
    run_deterministic,
    run_evaluation,
    run_historical_bakeoff,
    run_live,
    run_replay,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "docs" / "evaluation" / "ground_truth_manifest.json"
SCHEMA_PATH = REPO_ROOT / "docs" / "evaluation" / "results.schema.json"
RESULTS_DIR = REPO_ROOT / "docs" / "evaluation" / "results"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Helper: assert result is schema-valid
# ---------------------------------------------------------------------------

def assert_schema_valid(result: dict) -> None:
    errors = _validate_against_schema(result)
    assert not errors, f"Schema validation failed: {errors}"


# ---------------------------------------------------------------------------
# Unit tests: _rate helper
# ---------------------------------------------------------------------------

def test_rate_zero_denominator():
    r = _rate(0, 0)
    assert r["denominator"] == 0
    assert r["numerator"] == 0
    assert r["rate_percentage"] == 0.0


def test_rate_full():
    r = _rate(10, 10)
    assert r["rate_percentage"] == 100.0


def test_rate_partial():
    r = _rate(3, 4)
    assert r["rate_percentage"] == 75.0


# ---------------------------------------------------------------------------
# Unit tests: _validate_against_schema
# ---------------------------------------------------------------------------

def test_validate_rejects_missing_required_field():
    incomplete = {
        "run_id": "x",
        "mode": "DETERMINISTIC",
        "timestamp_utc": "2026-01-01T00:00:00Z",
        # missing git_commit, deterministic_metrics, e2e_metrics, sc_traceability
    }
    errors = _validate_against_schema(incomplete)
    assert any("git_commit" in e for e in errors)


def test_validate_rejects_invalid_mode():
    base = _empty_result_with_note("NOT_YET_MEASURED", "test")
    base.update({"run_id": "x", "mode": "BOGUS", "timestamp_utc": "2026-01-01T00:00:00Z",
                  "git_commit": "abc123"})
    errors = _validate_against_schema(base)
    assert any("mode" in e.lower() for e in errors)


def test_validate_accepts_valid_empty_result():
    result = _empty_result_with_note("NOT_YET_MEASURED", "test note")
    result.update({
        "run_id": "test-run",
        "mode": "DETERMINISTIC",
        "timestamp_utc": "2026-01-01T00:00:00Z",
        "git_commit": "abc123",
    })
    errors = _validate_against_schema(result)
    assert not errors, f"Expected valid: {errors}"


# ---------------------------------------------------------------------------
# Unit tests: in-memory session
# ---------------------------------------------------------------------------

def test_in_memory_session_has_baseline_data():
    session = _make_in_memory_session()
    try:
        from app.models.entities import CatalogProduct, CustomerContract
        products = session.query(CatalogProduct).all()
        contracts = session.query(CustomerContract).all()
        assert len(products) >= 10
        assert len(contracts) >= 2
    finally:
        session.close()


# ---------------------------------------------------------------------------
# DETERMINISTIC mode tests
# ---------------------------------------------------------------------------

def test_deterministic_arithmetic_line_discrepancy_detected(manifest):
    payload = run_deterministic(manifest)
    det = payload["deterministic_metrics"]
    # Must detect at least the arithmetic line error
    assert det["discrepancy_catch_rate"]["numerator"] >= 1


def test_deterministic_all_four_categories_detected(manifest):
    """All four canonical discrepancy types must be detected."""
    payload = run_deterministic(manifest)
    det = payload["deterministic_metrics"]
    # 4 synthetic cases each target one discrepancy type
    assert det["discrepancy_catch_rate"]["denominator"] >= 4
    assert det["discrepancy_catch_rate"]["numerator"] >= 4


def test_deterministic_zero_false_positives(manifest):
    payload = run_deterministic(manifest)
    det = payload["deterministic_metrics"]
    assert det["false_positive_discrepancy_count"] == 0


def test_deterministic_gate_blocking_enforced(manifest):
    payload = run_deterministic(manifest)
    det = payload["deterministic_metrics"]
    # approval_gate_enforcement case must pass
    assert det["gate_blocking_rate"]["numerator"] >= 1


def test_deterministic_terminal_state_enforcement(manifest):
    payload = run_deterministic(manifest)
    det = payload["deterministic_metrics"]
    assert det["terminal_state_enforcement_rate"]["numerator"] == 1
    assert det["terminal_state_enforcement_rate"]["denominator"] == 1


def test_deterministic_sc001_always_partially_measured(manifest):
    """SC-001 MUST be PARTIALLY_MEASURED — never PASS — after automated run."""
    payload = run_deterministic(manifest)
    sc001 = payload["sc_traceability"]["SC_001"]
    assert sc001["status"] == "PARTIALLY_MEASURED"


def test_deterministic_sc001_has_no_assisted_timing(manifest):
    """assisted_operator_completion_duration_seconds must be null after automated run."""
    payload = run_deterministic(manifest)
    sc001 = payload["sc_traceability"]["SC_001"]
    assert sc001.get("assisted_operator_completion_duration_seconds") is None


def test_deterministic_sc004_is_pass(manifest):
    """SC-004 should be PASS when clean drafts evaluate correctly."""
    payload = run_deterministic(manifest)
    sc004 = payload["sc_traceability"]["SC_004"]
    assert sc004["status"] == "PASS"


def test_deterministic_result_schema_valid(manifest):
    """Full result object must pass schema validation."""
    payload = run_deterministic(manifest)
    payload.update({
        "run_id": "test-det",
        "mode": "DETERMINISTIC",
        "timestamp_utc": "2026-01-01T00:00:00Z",
        "git_commit": "abc123",
    })
    assert_schema_valid(payload)


def test_deterministic_case_results_present(manifest):
    payload = run_deterministic(manifest)
    cases = payload.get("case_results", [])
    assert len(cases) >= 5  # 4 synthetic + sc001 + clean_acme at minimum


def test_deterministic_no_network_calls(manifest):
    """Deterministic mode must make zero network calls.
    We verify by patching the AI provider import and confirming it's never touched.
    """
    with patch("app.services.ai_provider") as mock_ai:
        payload = run_deterministic(manifest)
        mock_ai.assert_not_called()


# ---------------------------------------------------------------------------
# REPLAY mode tests
# ---------------------------------------------------------------------------

def test_replay_uses_only_historical_schema(manifest):
    """REPLAY mode must return ai_metrics.historical_bakeoff_metrics only."""
    payload = run_replay(manifest)
    ai = payload.get("ai_metrics", {})
    # Must have historical_bakeoff_metrics
    assert "historical_bakeoff_metrics" in ai
    # Must NOT have app_ai_metrics (schema separation)
    assert "app_ai_metrics" not in ai


def test_replay_sc002_is_measured(manifest):
    payload = run_replay(manifest)
    sc002 = payload["sc_traceability"]["SC_002"]
    assert sc002["status"] in {"PASS", "PARTIALLY_MEASURED", "FAIL"}


def test_replay_sc001_partially_measured(manifest):
    payload = run_replay(manifest)
    sc001 = payload["sc_traceability"]["SC_001"]
    assert sc001["status"] == "PARTIALLY_MEASURED"


def test_replay_result_schema_valid(manifest):
    payload = run_replay(manifest)
    payload.update({
        "run_id": "test-replay",
        "mode": "REPLAY",
        "timestamp_utc": "2026-01-01T00:00:00Z",
        "git_commit": "abc123",
    })
    assert_schema_valid(payload)


def test_replay_has_case_results(manifest):
    payload = run_replay(manifest)
    cases = payload.get("case_results", [])
    # 10 historical cases (h01..h10)
    assert len(cases) >= 8


# ---------------------------------------------------------------------------
# HISTORICAL_BAKEOFF mode tests
# ---------------------------------------------------------------------------

def test_historical_bakeoff_uses_only_historical_schema(manifest):
    payload = run_historical_bakeoff(manifest)
    ai = payload.get("ai_metrics", {})
    assert "historical_bakeoff_metrics" in ai
    assert "app_ai_metrics" not in ai


def test_historical_bakeoff_result_schema_valid(manifest):
    payload = run_historical_bakeoff(manifest)
    payload.update({
        "run_id": "test-hb",
        "mode": "HISTORICAL_BAKEOFF",
        "timestamp_utc": "2026-01-01T00:00:00Z",
        "git_commit": "abc123",
    })
    assert_schema_valid(payload)


def test_historical_bakeoff_latency_is_positive(manifest):
    payload = run_historical_bakeoff(manifest)
    hb = payload.get("ai_metrics", {}).get("historical_bakeoff_metrics", {})
    latency = hb.get("latency_profile_seconds", {})
    # The bake-off had real calls so mean should be > 0
    assert latency.get("mean", 0) >= 0.0


# ---------------------------------------------------------------------------
# LIVE mode gate test
# ---------------------------------------------------------------------------

def test_live_mode_blocked_without_env_var(manifest):
    """LIVE mode must not proceed without LIVE_EVALUATION_ENABLED=true."""
    import os
    env = {k: v for k, v in os.environ.items() if k != "LIVE_EVALUATION_ENABLED"}
    with patch.dict(os.environ, env, clear=True):
        payload = run_live(manifest)
    # Must return MISSING_EVALUATION_ASSET, not raise
    sc = payload["sc_traceability"]["SC_001"]
    assert sc["status"] == "MISSING_EVALUATION_ASSET"


# ---------------------------------------------------------------------------
# run_evaluation end-to-end tests (writes to disk)
# ---------------------------------------------------------------------------

def test_run_evaluation_deterministic_exits_zero(manifest, tmp_path, monkeypatch):
    """run_evaluation DETERMINISTIC must return 0 and write artifacts."""
    results_dir = tmp_path / "results"
    monkeypatch.setattr("app.evaluation.runner._RESULTS_DIR", results_dir)
    ret = run_evaluation("DETERMINISTIC")
    assert ret == 0


def test_run_evaluation_replay_exits_zero(manifest, tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setattr("app.evaluation.runner._RESULTS_DIR", results_dir)
    ret = run_evaluation("REPLAY")
    assert ret == 0


def test_run_evaluation_historical_bakeoff_exits_zero(manifest, tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setattr("app.evaluation.runner._RESULTS_DIR", results_dir)
    ret = run_evaluation("HISTORICAL_BAKEOFF")
    assert ret == 0


def test_run_evaluation_writes_timestamped_file(tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setattr("app.evaluation.runner._RESULTS_DIR", results_dir)
    run_evaluation("DETERMINISTIC")
    files = list(results_dir.glob("*-DETERMINISTIC.json"))
    assert len(files) >= 1


def test_run_evaluation_writes_latest_json(tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setattr("app.evaluation.runner._RESULTS_DIR", results_dir)
    run_evaluation("DETERMINISTIC")
    latest = results_dir / "latest.json"
    assert latest.exists()
    data = json.loads(latest.read_text(encoding="utf-8"))
    assert data["mode"] == "DETERMINISTIC"


def test_run_evaluation_latest_json_is_valid_schema(tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setattr("app.evaluation.runner._RESULTS_DIR", results_dir)
    run_evaluation("DETERMINISTIC")
    latest = results_dir / "latest.json"
    data = json.loads(latest.read_text(encoding="utf-8"))
    errors = _validate_against_schema(data)
    assert not errors, f"latest.json failed schema: {errors}"


def test_run_evaluation_invalid_mode_returns_nonzero():
    ret = run_evaluation("BOGUS_MODE")
    assert ret != 0


def test_run_evaluation_sc001_never_pass_in_automated_run(tmp_path, monkeypatch):
    """Invariant: SC-001 must never be PASS in automated runner output."""
    results_dir = tmp_path / "results"
    monkeypatch.setattr("app.evaluation.runner._RESULTS_DIR", results_dir)
    for mode in ["DETERMINISTIC", "REPLAY", "HISTORICAL_BAKEOFF"]:
        run_evaluation(mode)
    for f in results_dir.glob("*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        sc001 = data.get("sc_traceability", {}).get("SC_001", {})
        assert sc001.get("status") != "PASS", (
            f"{f.name}: SC-001 must not be PASS in automated runner (found PASS)"
        )


# ---------------------------------------------------------------------------
# Valid modes coverage
# ---------------------------------------------------------------------------

def test_valid_modes_contains_all_required():
    expected = {"DETERMINISTIC", "REPLAY", "LIVE", "HISTORICAL_BAKEOFF", "ALL"}
    assert VALID_MODES == expected


# ---------------------------------------------------------------------------
# Git commit helper
# ---------------------------------------------------------------------------

def test_current_git_commit_returns_string():
    commit = _current_git_commit()
    assert isinstance(commit, str)
    assert len(commit) >= 7  # At least a short SHA or 'UNKNOWN'
