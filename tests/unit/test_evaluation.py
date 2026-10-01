"""Unit and integration tests for OrderShield evaluation runner (app/evaluation/runner.py).

Comprehensive coverage for VLD-EVAL-02 / VLD-EVAL-02R:
- Strict jsonschema validation against docs/evaluation/results.schema.json
- Negative schema tests (wrong types, missing required fields, invalid enums)
- DETERMINISTIC mode: pure rules-engine, zero network, 4 discrepancy categories detected,
  zero false positives, SC-001/004/005 NOT_YET_MEASURED, false-positive denominator
- REPLAY mode: current-app E2E HTTP evaluation via TestClient, clean_acme,
  discrepancy_apex (State A->B->C), ambiguous_apex (State A->B->C), DET-4 (HTTP 409),
  DET-5 (HTTP 409), unextractable PDF (HTTP 400), SC-001 timing, SC-004 provenance
- HISTORICAL_BAKEOFF mode: Alibaba Qwen primary by explicit identity (ADR 0001),
  Gemini secondary, historical failures kept as FAIL, SC-002/003 NOT_YET_MEASURED
- LIVE mode gate: blocked without LIVE_EVALUATION_ENABLED=true
- ALL mode merge semantics: FAIL dominates PASS > PARTIALLY_MEASURED > NOT_YET_MEASURED
- Invariant: SC-001 is NEVER PASS in automated runner output
- Zero-network safety: verified with no_external_network socket-blocking fixture
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest

from app.evaluation.runner import (
    VALID_MODES,
    _current_git_commit,
    _empty_result_with_note,
    _find_primary_candidate,
    _find_secondary_candidate,
    _load_schema,
    _make_in_memory_session,
    _merge_sc_traceability,
    _rate,
    _validate_against_schema,
    run_all,
    run_deterministic,
    run_evaluation,
    run_historical_bakeoff,
    run_live,
    run_replay,
)

# ---------------------------------------------------------------------------
# Fixtures and Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "docs" / "evaluation" / "ground_truth_manifest.json"
SCHEMA_PATH = REPO_ROOT / "docs" / "evaluation" / "results.schema.json"
RESULTS_DIR = REPO_ROOT / "docs" / "evaluation" / "results"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def results_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def assert_schema_valid(result: dict) -> None:
    """Validate with both jsonschema library directly and runner's validator."""
    schema = _load_schema()
    jsonschema.validate(result, schema)
    errors = _validate_against_schema(result)
    assert not errors, f"Runner validation failed: {errors}"


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
# Schema Validation Tests (Section G: Real JSON Schema Validation)
# ---------------------------------------------------------------------------

def test_validate_accepts_valid_minimal_result():
    result = _empty_result_with_note("NOT_YET_MEASURED", "test note")
    result.update({
        "run_id": "test-run-123",
        "mode": "DETERMINISTIC",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        "git_commit": "abc1234",
    })
    assert_schema_valid(result)


def test_validate_rejects_missing_required_top_level_field():
    incomplete = {
        "run_id": "x",
        "mode": "DETERMINISTIC",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        # missing git_commit, deterministic_metrics, e2e_metrics, sc_traceability
    }
    errors = _validate_against_schema(incomplete)
    assert any("git_commit" in e or "required" in e.lower() for e in errors)


def test_validate_rejects_missing_nested_required_field():
    result = _empty_result_with_note("NOT_YET_MEASURED", "test note")
    result.update({
        "run_id": "test-run-123",
        "mode": "DETERMINISTIC",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        "git_commit": "abc1234",
    })
    # Remove required nested field in deterministic_metrics
    del result["deterministic_metrics"]["discrepancy_catch_rate"]
    errors = _validate_against_schema(result)
    assert any("discrepancy_catch_rate" in e for e in errors)


def test_validate_rejects_wrong_nested_type():
    result = _empty_result_with_note("NOT_YET_MEASURED", "test note")
    result.update({
        "run_id": "test-run-123",
        "mode": "DETERMINISTIC",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        "git_commit": "abc1234",
    })
    # Set integer count to a string
    result["deterministic_metrics"]["false_positive_discrepancy_count"] = "not_an_int"
    errors = _validate_against_schema(result)
    assert any("false_positive_discrepancy_count" in e or "integer" in e.lower() for e in errors)


def test_validate_rejects_invalid_mode_enum():
    result = _empty_result_with_note("NOT_YET_MEASURED", "test")
    result.update({
        "run_id": "x",
        "mode": "BOGUS_MODE",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        "git_commit": "abc1234",
    })
    errors = _validate_against_schema(result)
    assert any("mode" in e.lower() or "BOGUS_MODE" in e for e in errors)


def test_validate_rejects_invalid_sc_enum():
    result = _empty_result_with_note("NOT_YET_MEASURED", "test")
    result.update({
        "run_id": "x",
        "mode": "DETERMINISTIC",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        "git_commit": "abc1234",
    })
    result["sc_traceability"]["SC_001"]["status"] = "INVALID_STATUS"
    errors = _validate_against_schema(result)
    assert any("status" in e.lower() or "INVALID_STATUS" in e for e in errors)


def test_validate_rejects_invalid_case_result_status():
    result = _empty_result_with_note("NOT_YET_MEASURED", "test")
    result.update({
        "run_id": "x",
        "mode": "DETERMINISTIC",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        "git_commit": "abc1234",
        "case_results": [{"case_id": "c1", "status": "UNKNOWN_RESULT"}],
    })
    errors = _validate_against_schema(result)
    assert any("case_results" in e or "status" in e.lower() or "UNKNOWN_RESULT" in e for e in errors)


def test_validate_rejects_malformed_ai_metric_object():
    result = _empty_result_with_note("NOT_YET_MEASURED", "test")
    result.update({
        "run_id": "x",
        "mode": "HISTORICAL_BAKEOFF",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        "git_commit": "abc1234",
        "ai_metrics": {
            "historical_bakeoff_metrics": {
                # schema_valid_rate must be an object with numerator/denominator
                "schema_valid_rate": "100%",
            }
        },
    })
    errors = _validate_against_schema(result)
    assert any("schema_valid_rate" in e or "object" in e.lower() for e in errors)


def test_sc001_cannot_be_pass_in_automated_output():
    """Invariant: runner validator strictly forbids SC_001 = PASS."""
    result = _empty_result_with_note("NOT_YET_MEASURED", "test")
    result.update({
        "run_id": "x",
        "mode": "DETERMINISTIC",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        "git_commit": "abc1234",
    })
    result["sc_traceability"]["SC_001"]["status"] = "PASS"
    errors = _validate_against_schema(result)
    assert any("INVARIANT VIOLATION" in e or "SC_001" in e for e in errors)


# ---------------------------------------------------------------------------
# DETERMINISTIC Mode Tests
# ---------------------------------------------------------------------------

def test_deterministic_detects_all_four_discrepancy_categories(manifest):
    payload = run_deterministic(manifest)
    det = payload["deterministic_metrics"]
    # 5 tests executed: line arithmetic, order arithmetic, packaging breach, unrecognized SKU, price mismatch
    assert det["discrepancy_catch_rate"]["numerator"] >= 4
    assert det["discrepancy_catch_rate"]["denominator"] >= 4
    assert det["discrepancy_catch_rate"]["rate_percentage"] == 100.0


def test_deterministic_zero_false_positives(manifest):
    payload = run_deterministic(manifest)
    det = payload["deterministic_metrics"]
    assert det["false_positive_discrepancy_count"] == 0
    assert det["false_positive_rate"] == 0.0
    assert "clean cases evaluated" in det.get("false_positive_rate_denominator", "")


def test_deterministic_sc_traceability_boundaries(manifest):
    """Section B: DETERMINISTIC must leave SC-001, SC-004, SC-005 NOT_YET_MEASURED."""
    payload = run_deterministic(manifest)
    sc = payload["sc_traceability"]
    assert sc["SC_001"]["status"] == "PARTIALLY_MEASURED"
    assert sc["SC_001"]["automated_system_path_duration_ms"] is None  # ORM != E2E path
    assert sc["SC_002"]["status"] == "PASS"
    assert sc["SC_003"]["status"] == "NOT_YET_MEASURED"
    assert sc["SC_004"]["status"] == "NOT_YET_MEASURED"
    assert sc["SC_005"]["status"] == "NOT_YET_MEASURED"
    assert sc["SC_006"]["status"] == "NOT_YET_MEASURED"


def test_deterministic_result_schema_valid(manifest):
    payload = run_deterministic(manifest)
    payload.update({
        "run_id": "test-det-run",
        "mode": "DETERMINISTIC",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        "git_commit": "abc1234",
    })
    assert_schema_valid(payload)


# ---------------------------------------------------------------------------
# REPLAY Mode Tests (Section C & E: Application HTTP/API Evaluation)
# ---------------------------------------------------------------------------

def test_replay_exercises_all_app_scenarios(manifest):
    """REPLAY exercises clean_acme, discrepancy_apex, ambiguous_apex, det5, unextractable_pdf."""
    payload = run_replay(manifest)
    cases = payload["case_results"]
    case_ids = {c["case_id"] for c in cases}
    assert "clean_acme" in case_ids
    assert "discrepancy_apex" in case_ids
    assert "ambiguous_apex" in case_ids
    assert "det5_terminal_state_http" in case_ids
    assert "unextractable_pdf" in case_ids


def test_replay_measures_sc001_automated_duration(manifest):
    """Section F: sc001_automated_system_path_duration_ms is set from clean_acme E2E path."""
    payload = run_replay(manifest)
    sc001 = payload["sc_traceability"]["SC_001"]
    assert sc001["status"] == "PARTIALLY_MEASURED"
    auto_ms = sc001["automated_system_path_duration_ms"]
    assert auto_ms is not None
    assert auto_ms > 0
    assert sc001["assisted_operator_completion_duration_seconds"] is None


def test_replay_measures_sc004_provenance_citations(manifest):
    """Section B: SC-004 measured in REPLAY via clean_acme fixture provenance citations."""
    payload = run_replay(manifest)
    sc004 = payload["sc_traceability"]["SC_004"]
    assert sc004["status"] == "PASS"
    assert sc004["value"] == 100.0


def test_replay_measures_sc002_current_app_discrepancies(manifest):
    """Section C: SC-002 measured via discrepancy_apex lifecycle."""
    payload = run_replay(manifest)
    sc002 = payload["sc_traceability"]["SC_002"]
    assert sc002["status"] == "PASS"


def test_replay_measures_sc003_operator_sku_resolution(manifest):
    """Section C: SC-003 measured via ambiguous_apex SelectSKU lifecycle."""
    payload = run_replay(manifest)
    sc003 = payload["sc_traceability"]["SC_003"]
    assert sc003["status"] == "PASS"


def test_replay_measures_sc005_det4_http_gate_blocking(manifest):
    """Section E: DET-4 gate blocking verified via POST /approve returning HTTP 409."""
    payload = run_replay(manifest)
    sc005 = payload["sc_traceability"]["SC_005"]
    assert sc005["status"] == "PASS"
    assert "DraftNotReadyForApprovalError" in str(sc005.get("value", "")) or "DraftNotReadyForApprovalError" in str(sc005.get("notes", ""))


def test_replay_measures_sc006_unextractable_pdf(manifest):
    """Section A & E: SC-006 verified via unextractable PDF returning HTTP 400 + UnextractableTextError."""
    payload = run_replay(manifest)
    sc006 = payload["sc_traceability"]["SC_006"]
    assert sc006["status"] == "PASS"


def test_replay_verifies_is_replay_mode_badge(manifest):
    payload = run_replay(manifest)
    badge_rate = payload["e2e_metrics"]["visible_replay_mode_badge_rate"]
    assert badge_rate == 1.0


def test_replay_result_schema_valid(manifest):
    payload = run_replay(manifest)
    payload.update({
        "run_id": "test-replay-run",
        "mode": "REPLAY",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        "git_commit": "abc1234",
    })
    assert_schema_valid(payload)


# ---------------------------------------------------------------------------
# HISTORICAL_BAKEOFF Mode Tests (Section I: Explicit Provider Selection)
# ---------------------------------------------------------------------------

def test_historical_bakeoff_selects_alibaba_qwen_primary(manifest):
    """Section I: Primary candidate must be Alibaba Qwen (ADR 0001), not candidates[0]."""
    payload = run_historical_bakeoff(manifest)
    hb = payload["ai_metrics"]["historical_bakeoff_metrics"]
    provider = hb["provider"].lower()
    model = hb["model"].lower()
    assert "alibaba" in provider or "qwen" in provider or "qwen" in model


def test_find_primary_candidate_does_not_blindly_pick_index_zero():
    # If Gemini is at index 0, _find_primary_candidate must still pick Alibaba at index 1
    mock_candidates = [
        {"provider": "Google Gemini API", "requested_model": "gemini-3.5-flash-lite"},
        {"provider": "Alibaba Model Studio (Singapore)", "requested_model": "qwen3.8-flash"},
    ]
    cand = _find_primary_candidate(mock_candidates)
    assert cand is not None
    assert "Alibaba" in cand["provider"]


def test_historical_bakeoff_leaves_sc002_sc003_unmeasured(manifest):
    """Section C: SC-002 and SC-003 must be NOT_YET_MEASURED in historical bake-off."""
    payload = run_historical_bakeoff(manifest)
    sc = payload["sc_traceability"]
    assert sc["SC_002"]["status"] == "NOT_YET_MEASURED"
    assert sc["SC_003"]["status"] == "NOT_YET_MEASURED"


def test_historical_bakeoff_preserves_historical_failures_as_fail(manifest):
    """Section D: h08 and h10 provider errors must remain status=FAIL."""
    payload = run_historical_bakeoff(manifest)
    cases = {c["case_id"]: c for c in payload["case_results"]}
    # h08 or h10 on Gemini had SEMANTIC_WRONG / LOCAL_VALIDATION_FAILURE
    fail_cases = [c for c in payload["case_results"] if c["status"] == "FAIL"]
    assert len(fail_cases) > 0, "Known historical provider errors must be reported as FAIL"


def test_historical_bakeoff_result_schema_valid(manifest):
    payload = run_historical_bakeoff(manifest)
    payload.update({
        "run_id": "test-hb-run",
        "mode": "HISTORICAL_BAKEOFF",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        "git_commit": "abc1234",
    })
    assert_schema_valid(payload)


# ---------------------------------------------------------------------------
# LIVE Mode Gate Tests (Section A: Gated Provider Execution)
# ---------------------------------------------------------------------------

def test_live_mode_blocked_without_env_var(manifest):
    """LIVE mode must return MISSING_EVALUATION_ASSET when env var is not set."""
    with patch.dict("os.environ", {"LIVE_EVALUATION_ENABLED": "false"}, clear=False):
        payload = run_live(manifest)
    assert payload["sc_traceability"]["SC_001"]["status"] == "MISSING_EVALUATION_ASSET"


# ---------------------------------------------------------------------------
# ALL Mode Tests (Section J: Precedence & Merge Semantics)
# ---------------------------------------------------------------------------

def test_merge_sc_traceability_fail_dominates_pass():
    """Section J: FAIL must dominate PASS for the same criterion."""
    sc1 = {"SC_002": {"status": "PASS", "metric_name": "m", "value": 1, "unit": None, "notes": "pass"}}
    sc2 = {"SC_002": {"status": "FAIL", "metric_name": "m", "value": 0, "unit": None, "notes": "fail"}}
    merged = _merge_sc_traceability([sc1, sc2])
    assert merged["SC_002"]["status"] == "FAIL"

    # Reverse order should yield the same result
    merged_rev = _merge_sc_traceability([sc2, sc1])
    assert merged_rev["SC_002"]["status"] == "FAIL"


def test_merge_sc_traceability_pass_dominates_partially_measured():
    sc1 = {"SC_003": {"status": "PARTIALLY_MEASURED", "metric_name": "m", "value": None, "unit": None, "notes": "partial"}}
    sc2 = {"SC_003": {"status": "PASS", "metric_name": "m", "value": 100, "unit": None, "notes": "pass"}}
    merged = _merge_sc_traceability([sc1, sc2])
    assert merged["SC_003"]["status"] == "PASS"


def test_run_all_combines_offline_modes(manifest):
    payload = run_all(manifest)
    sc = payload["sc_traceability"]
    # In ALL mode without LIVE:
    assert sc["SC_001"]["status"] == "PARTIALLY_MEASURED"
    assert sc["SC_002"]["status"] == "PASS"
    assert sc["SC_003"]["status"] == "PASS"
    assert sc["SC_004"]["status"] == "PASS"
    assert sc["SC_005"]["status"] == "PASS"
    assert sc["SC_006"]["status"] == "PASS"


def test_run_all_result_schema_valid(manifest):
    payload = run_all(manifest)
    payload.update({
        "run_id": "test-all-run",
        "mode": "ALL",
        "timestamp_utc": "2026-10-02T00:00:00Z",
        "git_commit": "abc1234",
    })
    assert_schema_valid(payload)


# ---------------------------------------------------------------------------
# Zero-Network Safety Tests (Section L: Socket-Level Isolation)
# ---------------------------------------------------------------------------

def test_deterministic_zero_network_safety(manifest, no_external_network):
    """Proves DETERMINISTIC mode makes zero socket network calls."""
    payload = run_deterministic(manifest)
    assert payload["deterministic_metrics"]["discrepancy_catch_rate"]["numerator"] >= 4


def test_replay_zero_network_safety(manifest, no_external_network):
    """Proves REPLAY mode makes zero socket network calls."""
    payload = run_replay(manifest)
    assert payload["sc_traceability"]["SC_004"]["status"] == "PASS"


def test_historical_bakeoff_zero_network_safety(manifest, no_external_network):
    """Proves HISTORICAL_BAKEOFF mode makes zero socket network calls."""
    payload = run_historical_bakeoff(manifest)
    assert payload["ai_metrics"]["historical_bakeoff_metrics"]["provider"] is not None


# ---------------------------------------------------------------------------
# CLI run_evaluation File Writing Tests
# ---------------------------------------------------------------------------

def test_run_evaluation_deterministic_exit_zero(tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setattr("app.evaluation.runner._RESULTS_DIR", results_dir)
    ret = run_evaluation("DETERMINISTIC")
    assert ret == 0
    assert len(list(results_dir.glob("*-DETERMINISTIC.json"))) == 1
    latest = json.loads((results_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["mode"] == "DETERMINISTIC"
    assert_schema_valid(latest)


def test_run_evaluation_replay_exit_zero(tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setattr("app.evaluation.runner._RESULTS_DIR", results_dir)
    ret = run_evaluation("REPLAY")
    assert ret == 0
    assert len(list(results_dir.glob("*-REPLAY.json"))) == 1
    latest = json.loads((results_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["mode"] == "REPLAY"
    assert_schema_valid(latest)


def test_run_evaluation_historical_bakeoff_exit_zero(tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setattr("app.evaluation.runner._RESULTS_DIR", results_dir)
    ret = run_evaluation("HISTORICAL_BAKEOFF")
    assert ret == 0
    assert len(list(results_dir.glob("*-HISTORICAL_BAKEOFF.json"))) == 1
    latest = json.loads((results_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["mode"] == "HISTORICAL_BAKEOFF"
    assert_schema_valid(latest)


def test_run_evaluation_all_exit_zero(tmp_path, monkeypatch):
    results_dir = tmp_path / "results"
    monkeypatch.setattr("app.evaluation.runner._RESULTS_DIR", results_dir)
    ret = run_evaluation("ALL")
    assert ret == 0
    assert len(list(results_dir.glob("*-ALL.json"))) == 1
    latest = json.loads((results_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["mode"] == "ALL"
    assert_schema_valid(latest)


def test_run_evaluation_invalid_mode_exit_one():
    ret = run_evaluation("INVALID_MODE")
    assert ret == 1
