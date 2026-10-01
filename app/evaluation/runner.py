"""OrderShield Reproducible Evaluation Runner — VLD-EVAL-02 / VLD-EVAL-02R.

Implements:
    python -m app.cli evaluate --mode=<MODE>

Modes:
    DETERMINISTIC        Zero-network: pure rules-engine evaluation.
                         Exercises arithmetic, contract pricing, MOQ/package,
                         CatalogMatchingMismatch, PriceMismatch detection.
                         Does NOT measure SC-001, SC-004, SC-005 (HTTP-level only).

    REPLAY               Zero-network: current application HTTP/API evaluation.
                         Uses FastAPI TestClient with isolated in-memory DB.
                         Exercises app fixture paths (clean_acme, discrepancy_apex,
                         ambiguous_apex), DET-4/DET-5 gate enforcement, provenance,
                         is_replay_mode badge, approval lifecycle.

    HISTORICAL_BAKEOFF   Zero-network: historical spikes/ordershield/** evidence only.
                         Primary provider: Alibaba Model Studio / Qwen (ADR 0001).
                         Historical metrics MUST NOT be used as current-app metrics.
                         SC-002/SC-003 NOT_YET_MEASURED (historical != current-app E2E).

    LIVE                 Network: calls actual AI provider. Gated by
                         LIVE_EVALUATION_ENABLED=true. Not run automatically.

    ALL                  DETERMINISTIC + REPLAY + HISTORICAL_BAKEOFF.
                         LIVE only if explicitly enabled.
                         FAIL > PASS > PARTIALLY_MEASURED > NOT_YET_MEASURED.

Invariants:
    - SC-001 is always PARTIALLY_MEASURED in automated runs (never PASS).
    - Zero network calls for DETERMINISTIC / REPLAY / HISTORICAL_BAKEOFF.
    - Historical and current-app schemas are never co-mingled.
    - All result artifacts validate against docs/evaluation/results.schema.json.
    - Primary historical provider = Alibaba Qwen (ADR 0001), never candidates[0].
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema

# ---------------------------------------------------------------------------
# Repository root & key paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _REPO_ROOT / "docs" / "evaluation" / "ground_truth_manifest.json"
_SCHEMA_PATH = _REPO_ROOT / "docs" / "evaluation" / "results.schema.json"
_RESULTS_DIR = _REPO_ROOT / "docs" / "evaluation" / "results"
_EXPECTED_JSON = _REPO_ROOT / "spikes" / "ordershield" / "live" / "expected.json"
_PHASE2_SUMMARY = _REPO_ROOT / "spikes" / "ordershield" / "provider_bakeoff" / "phase2_summary.json"

# ---------------------------------------------------------------------------
# Supported evaluation modes
# ---------------------------------------------------------------------------

VALID_MODES = {"DETERMINISTIC", "REPLAY", "LIVE", "HISTORICAL_BAKEOFF", "ALL"}

# ---------------------------------------------------------------------------
# SC status constants and merge precedence
# ---------------------------------------------------------------------------

_SC_STATUS_RANK: dict[str, int] = {
    "MISSING_EVALUATION_ASSET": 0,
    "NOT_YET_MEASURED": 0,
    "PARTIALLY_MEASURED": 1,
    "PASS": 2,
    "FAIL": 3,  # FAIL dominates PASS
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    """Build a rate metric object."""
    pct = round(100.0 * numerator / denominator, 4) if denominator > 0 else 0.0
    return {"numerator": numerator, "denominator": denominator, "rate_percentage": pct}


def _cents(dollar_str: str) -> int:
    """Convert dollar string to integer cents."""
    return int(Decimal(dollar_str) * 100)


def _sc_not_measured(metric_name: str, notes: str) -> dict[str, Any]:
    return {"status": "NOT_YET_MEASURED", "metric_name": metric_name,
            "value": None, "unit": None, "notes": notes}


def _sc_missing(metric_name: str, notes: str) -> dict[str, Any]:
    return {"status": "MISSING_EVALUATION_ASSET", "metric_name": metric_name,
            "value": None, "unit": None, "notes": notes}


def _sc_partially_measured(
    metric_name: str,
    value: Any,
    unit: str | None,
    notes: str,
    auto_ms: float | None = None,
) -> dict[str, Any]:
    return {
        "status": "PARTIALLY_MEASURED",
        "metric_name": metric_name,
        "value": value,
        "unit": unit,
        "automated_system_path_duration_ms": auto_ms,
        "assisted_operator_completion_duration_seconds": None,
        "notes": notes,
    }


def _sc_result(metric_name: str, status: str, value: Any, unit: str | None, notes: str) -> dict[str, Any]:
    return {"status": status, "metric_name": metric_name, "value": value, "unit": unit, "notes": notes}


# ---------------------------------------------------------------------------
# Git commit helper
# ---------------------------------------------------------------------------

def _current_git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, cwd=str(_REPO_ROOT),
        )
        return result.stdout.strip()
    except Exception:
        return "UNKNOWN"


# ---------------------------------------------------------------------------
# JSON Schema validation (real jsonschema)
# ---------------------------------------------------------------------------

def _load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate_against_schema(result: dict[str, Any]) -> list[str]:
    """Validate result using jsonschema against docs/evaluation/results.schema.json.

    Returns list of error strings (empty = valid).
    The jsonschema validation is the primary gate.
    Additional field-level checks are appended as supplemental diagnostics.
    """
    errors: list[str] = []

    # --- Primary: jsonschema validation ---
    if _SCHEMA_PATH.exists():
        schema = _load_schema()
        try:
            jsonschema.validate(result, schema)
        except jsonschema.ValidationError as exc:
            errors.append(f"Schema validation: {exc.message} (path: {list(exc.absolute_path)})")
        except jsonschema.SchemaError as exc:
            errors.append(f"Schema file error: {exc.message}")
    else:
        errors.append(f"results.schema.json not found at {_SCHEMA_PATH}")

    # --- Supplemental: invariant checks ---
    sc = result.get("sc_traceability", {})
    valid_statuses = {"NOT_YET_MEASURED", "MISSING_EVALUATION_ASSET",
                      "PARTIALLY_MEASURED", "PASS", "FAIL"}
    for sc_key in ["SC_001", "SC_002", "SC_003", "SC_004", "SC_005", "SC_006"]:
        if sc_key not in sc:
            if not errors:  # only add if jsonschema didn't already catch it
                errors.append(f"Missing sc_traceability.{sc_key}")
        elif sc[sc_key].get("status") not in valid_statuses:
            errors.append(f"Invalid status {sc[sc_key].get('status')!r} for {sc_key}")

    # SC-001 invariant: must never be PASS in automated runner
    sc001 = sc.get("SC_001", {})
    if sc001.get("status") == "PASS":
        errors.append("INVARIANT VIOLATION: SC_001 must never be PASS in automated runner output")

    return errors


# ---------------------------------------------------------------------------
# Result I/O
# ---------------------------------------------------------------------------

def _write_result(result: dict[str, Any], mode: str) -> tuple[Path, Path]:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    fname = f"{ts}-{mode}.json"
    timestamped = _RESULTS_DIR / fname
    latest = _RESULTS_DIR / "latest.json"
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    timestamped.write_text(payload, encoding="utf-8")
    latest.write_text(payload, encoding="utf-8")
    return timestamped, latest


# ---------------------------------------------------------------------------
# In-memory DB session (for DETERMINISTIC)
# ---------------------------------------------------------------------------

def _make_in_memory_session():  # type: ignore[return]
    """Create a fresh in-memory SQLite session seeded with baseline catalog data."""
    from sqlalchemy import create_engine
    from sqlalchemy import event as sa_event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.models.entities  # noqa: F401
    from app.cli import seed_baseline
    from app.database import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(engine, "connect")
    def _set_pragma(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    seed_baseline(session)
    session.commit()
    return session


# ---------------------------------------------------------------------------
# In-memory TestClient (for REPLAY)
# ---------------------------------------------------------------------------

def _make_eval_client():
    """Create an isolated FastAPI TestClient with in-memory DB for REPLAY evaluation.

    This is entirely independent of test conftest fixtures. Each REPLAY run
    creates a fresh engine, seeds baseline data, and tears down after evaluation.
    """
    from sqlalchemy import create_engine
    from sqlalchemy import event as sa_event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from fastapi.testclient import TestClient

    import app.models.entities  # noqa: F401
    from app.cli import seed_baseline
    from app.database import Base, get_db
    from app.main import app

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(engine, "connect")
    def _fk(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    seed_baseline(session)
    session.commit()

    # Override DB dependency for this evaluation run
    def _get_db_override():
        yield session

    app.dependency_overrides[get_db] = _get_db_override

    client = TestClient(app, raise_server_exceptions=False)
    return client, session, engine, app, get_db


def _cleanup_eval_client(app, get_db, session, engine) -> None:
    """Remove the dependency override and close resources."""
    from app.database import Base
    app.dependency_overrides.pop(get_db, None)
    session.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


# ---------------------------------------------------------------------------
# Draft builder helpers for DETERMINISTIC mode
# ---------------------------------------------------------------------------

def _raw_text(po_number: str) -> str:
    return (
        f"PURCHASE ORDER\n"
        f"Customer: Test Customer\n"
        f"PO: {po_number}\n"
        f"\n"
        f"Item 1 qty 1 price $1.00 total $1.00\n"
    )


def _build_single_line_draft(session, *, customer_id, customer_name, po_number,
                               description, quantity, unit_price_cents, line_total_cents,
                               sku, sku_confidence, sku_source):
    from app.models.entities import DraftLineItem, OrderDraft, PurchaseOrderDocument
    doc = PurchaseOrderDocument(
        filename=f"{po_number}.txt", content_type="text/plain",
        raw_text=_raw_text(po_number), status="Ingested",
    )
    draft = OrderDraft(
        document=doc, customer_id=customer_id,
        customer_name_extracted=customer_name, po_number_extracted=po_number,
        status="Ingested", is_replay_mode=False,
        line_items=[DraftLineItem(
            line_number=1, customer_description=description,
            extracted_quantity=quantity, extracted_unit_price_cents=unit_price_cents,
            extracted_line_total_cents=line_total_cents, matched_sku=sku,
            sku_confidence=sku_confidence, sku_resolution_source=sku_source, status="Active",
        )],
    )
    session.add(draft)
    session.flush()
    return draft


def _build_two_line_draft(session, *, customer_id, customer_name, po_number,
                           stated_order_total_cents):
    from app.models.entities import DraftLineItem, OrderDraft, PurchaseOrderDocument
    raw = _raw_text(po_number)
    doc = PurchaseOrderDocument(
        filename=f"{po_number}.txt", content_type="text/plain",
        raw_text=raw, status="Ingested",
    )
    draft = OrderDraft(
        document=doc, customer_id=customer_id,
        customer_name_extracted=customer_name, po_number_extracted=po_number,
        status="Ingested", is_replay_mode=False,
        extracted_order_total_cents=stated_order_total_cents,
        line_items=[
            DraftLineItem(
                line_number=1, customer_description="18in stretch film",
                extracted_quantity=10, extracted_unit_price_cents=2500,
                extracted_line_total_cents=25000, matched_sku="SKU-WRAP-18",
                sku_confidence="High", sku_resolution_source="AI_HIGH_CONFIDENCE", status="Active",
            ),
            DraftLineItem(
                line_number=2, customer_description="Standard Pallet Wrap 15in",
                extracted_quantity=5, extracted_unit_price_cents=2000,
                extracted_line_total_cents=10000, matched_sku="SKU-WRAP-15",
                sku_confidence="High", sku_resolution_source="AI_HIGH_CONFIDENCE", status="Active",
            ),
        ],
    )
    session.add(draft)
    session.flush()
    return draft


# ===========================================================================
# DETERMINISTIC mode
# ===========================================================================

def run_deterministic(manifest: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    """Pure rules-engine evaluation — zero network calls.

    Measures: arithmetic errors, MOQ/packaging breaches, CatalogMatchingMismatch,
    PriceMismatch detection, service-layer gate blocking, terminal-state enforcement.

    Does NOT measure: SC-001 (no E2E system path), SC-004 (requires HTTP provenance),
    SC-005 (HTTP-level, measured in REPLAY).
    """
    from app.services.reconciliation import evaluate_clean_draft

    case_results: list[dict[str, Any]] = []

    # Metric counters
    disc_detected = 0     # discrepancy cases where expected type was found
    disc_expected = 0     # total cases expecting a discrepancy
    clean_cases_evaluated = 0  # cases expecting 0 discrepancies
    false_positive_count = 0   # clean cases that incorrectly became Needs Review with flags
    gate_block_correct = 0
    gate_block_total = 0
    terminal_correct = 0
    terminal_total = 0

    # -----------------------------------------------------------------------
    # Synthetic deterministic suite — 4 discrepancy detection cases
    # -----------------------------------------------------------------------
    synthetic_cases = manifest["corpora"]["synthetic_deterministic_suite"]

    for case in synthetic_cases:
        case_id = case["case_id"]
        t0 = time.perf_counter()
        status = "SKIPPED"
        detail: dict[str, Any] = {}

        if case_id == "arithmetic_line_error":
            disc_expected += 1
            session = _make_in_memory_session()
            try:
                draft = _build_single_line_draft(
                    session, customer_id="CUST-ACME",
                    customer_name="Acme Industrial Supplies", po_number="PO-SYNTH-001",
                    description="18in stretch film heavy duty",
                    quantity=10, unit_price_cents=2500, line_total_cents=24000,  # 10*2500=25000, stated 24000
                    sku="SKU-WRAP-18", sku_confidence="High", sku_source="AI_HIGH_CONFIDENCE",
                )
                result = evaluate_clean_draft(session, draft)
                unresolved_types = [f.discrepancy_type for f in result.discrepancy_flags
                                     if f.resolution_state == "Unresolved"]
                if "ArithmeticMismatch" in unresolved_types:
                    disc_detected += 1
                    status = "PASS"
                else:
                    status = "FAIL"
                detail = {"unresolved": unresolved_types, "draft_status": result.status}
            finally:
                session.close()

        elif case_id == "arithmetic_order_total_error":
            disc_expected += 1
            session = _make_in_memory_session()
            try:
                draft = _build_two_line_draft(
                    session, customer_id="CUST-ACME",
                    customer_name="Acme Industrial Supplies", po_number="PO-SYNTH-002",
                    stated_order_total_cents=36000,  # correct=35000
                )
                result = evaluate_clean_draft(session, draft)
                unresolved_types = [f.discrepancy_type for f in result.discrepancy_flags
                                     if f.resolution_state == "Unresolved"]
                if "ArithmeticMismatch" in unresolved_types:
                    disc_detected += 1
                    status = "PASS"
                else:
                    status = "FAIL"
                detail = {"unresolved": unresolved_types, "draft_status": result.status}
            finally:
                session.close()

        elif case_id == "package_increment_breach":
            disc_expected += 1
            session = _make_in_memory_session()
            try:
                draft = _build_single_line_draft(
                    session, customer_id="CUST-ACME",
                    customer_name="Acme Industrial Supplies", po_number="PO-SYNTH-003",
                    description="Heavy Duty Packaging Tape",
                    quantity=7, unit_price_cents=350, line_total_cents=2450,  # package_increment=6
                    sku="SKU-TAPE-02", sku_confidence="High", sku_source="AI_HIGH_CONFIDENCE",
                )
                result = evaluate_clean_draft(session, draft)
                unresolved_types = [f.discrepancy_type for f in result.discrepancy_flags
                                     if f.resolution_state == "Unresolved"]
                if "QuantityOrPackagingBreach" in unresolved_types:
                    disc_detected += 1
                    status = "PASS"
                else:
                    status = "FAIL"
                detail = {"unresolved": unresolved_types, "draft_status": result.status}
            finally:
                session.close()

        elif case_id == "unrecognized_sku_rejection":
            disc_expected += 1
            session = _make_in_memory_session()
            try:
                draft = _build_single_line_draft(
                    session, customer_id="CUST-ACME",
                    customer_name="Acme Industrial Supplies", po_number="PO-SYNTH-004",
                    description="Titanium Cryogenic Valve Assembly X-900",
                    quantity=1, unit_price_cents=100000, line_total_cents=100000,
                    sku=None, sku_confidence="Unrecognized", sku_source="NONE",
                )
                result = evaluate_clean_draft(session, draft)
                unresolved_types = [f.discrepancy_type for f in result.discrepancy_flags
                                     if f.resolution_state == "Unresolved"]
                if "CatalogMatchingMismatch" in unresolved_types:
                    disc_detected += 1
                    status = "PASS"
                else:
                    status = "FAIL"
                detail = {"unresolved": unresolved_types, "draft_status": result.status}
            finally:
                session.close()

        elif case_id == "approval_gate_enforcement":
            # Service-layer gate: verify draft with PriceMismatch stays Needs Review
            # HTTP-level DET-4 (HTTP 409) is measured in REPLAY mode
            gate_block_total += 1
            session = _make_in_memory_session()
            try:
                draft = _build_single_line_draft(
                    session, customer_id="CUST-ACME",
                    customer_name="Acme Industrial Supplies", po_number="PO-SYNTH-005",
                    description="18in stretch film heavy duty",
                    quantity=10, unit_price_cents=1800, line_total_cents=18000,  # price mismatch
                    sku="SKU-WRAP-18", sku_confidence="High", sku_source="AI_HIGH_CONFIDENCE",
                )
                result = evaluate_clean_draft(session, draft)
                if result.status == "Needs Review":
                    gate_block_correct += 1
                    status = "PASS"
                    detail = {"draft_status": result.status, "note": "Service-layer gate confirmed. HTTP 409 measured in REPLAY."}
                else:
                    status = "FAIL"
                    detail = {"draft_status": result.status}
            finally:
                session.close()

        else:
            status = "SKIPPED"

        elapsed_ms = (time.perf_counter() - t0) * 1000
        case_results.append({
            "case_id": case_id, "status": status,
            "latency_seconds": round(elapsed_ms / 1000, 4),
            "details": detail,
        })
        if verbose:
            print(f"  [DETERMINISTIC] {case_id}: {status}")

    # -----------------------------------------------------------------------
    # Additional: PriceMismatch detection (not in synthetic suite)
    # -----------------------------------------------------------------------
    disc_expected += 1
    session = _make_in_memory_session()
    try:
        draft = _build_single_line_draft(
            session, customer_id="CUST-ACME",
            customer_name="Acme Industrial Supplies", po_number="PO-SYNTH-PRICE",
            description="18in stretch film heavy duty",
            quantity=10, unit_price_cents=2499, line_total_cents=24990,  # correct tier=2500
            sku="SKU-WRAP-18", sku_confidence="High", sku_source="AI_HIGH_CONFIDENCE",
        )
        result = evaluate_clean_draft(session, draft)
        unresolved_types = [f.discrepancy_type for f in result.discrepancy_flags
                             if f.resolution_state == "Unresolved"]
        if "PriceMismatch" in unresolved_types:
            disc_detected += 1
            status = "PASS"
        else:
            status = "FAIL"
        case_results.append({
            "case_id": "price_mismatch_detection",
            "status": status,
            "latency_seconds": round((time.perf_counter() - t0) * 1000 / 1000, 4),
            "details": {"unresolved": unresolved_types},
        })
        if verbose:
            print(f"  [DETERMINISTIC] price_mismatch_detection: {status}")
    finally:
        session.close()

    # -----------------------------------------------------------------------
    # Clean case: verify clean draft (0 discrepancies)
    # -----------------------------------------------------------------------
    clean_cases_evaluated += 1
    session = _make_in_memory_session()
    t0 = time.perf_counter()
    try:
        draft = _build_single_line_draft(
            session, customer_id="CUST-ACME",
            customer_name="Acme Industrial Supplies", po_number="PO-SYNTH-CLEAN",
            description="18in stretch film heavy duty",
            quantity=10, unit_price_cents=2500, line_total_cents=25000,  # exact match
            sku="SKU-WRAP-18", sku_confidence="High", sku_source="AI_HIGH_CONFIDENCE",
        )
        result = evaluate_clean_draft(session, draft)
        unresolved = [f for f in result.discrepancy_flags if f.resolution_state == "Unresolved"]
        if result.status == "Ready for Approval" and not unresolved:
            clean_status = "PASS"
        else:
            clean_status = "FAIL"
            false_positive_count += len(unresolved)
        case_results.append({
            "case_id": "clean_draft_no_false_positives",
            "status": clean_status,
            "latency_seconds": round((time.perf_counter() - t0) * 1000 / 1000, 4),
            "details": {"draft_status": result.status, "unresolved_count": len(unresolved)},
        })
        if verbose:
            print(f"  [DETERMINISTIC] clean_draft_no_false_positives: {clean_status}")
    finally:
        session.close()

    # -----------------------------------------------------------------------
    # Terminal-state enforcement (service layer)
    # -----------------------------------------------------------------------
    terminal_total += 1
    session = _make_in_memory_session()
    try:
        draft = _build_single_line_draft(
            session, customer_id="CUST-ACME",
            customer_name="Acme Industrial Supplies", po_number="PO-TERM-001",
            description="18in stretch film heavy duty",
            quantity=10, unit_price_cents=2500, line_total_cents=25000,
            sku="SKU-WRAP-18", sku_confidence="High", sku_source="AI_HIGH_CONFIDENCE",
        )
        draft.status = "Approved"
        session.flush()
        raised = False
        try:
            evaluate_clean_draft(session, draft)
        except ValueError:
            raised = True
        if raised:
            terminal_correct += 1
            term_status = "PASS"
        else:
            term_status = "FAIL"
        case_results.append({
            "case_id": "terminal_state_service_layer",
            "status": term_status,
            "latency_seconds": None,
            "details": {"note": "Service-layer ValueError. HTTP 409 TerminalDraftConflictError is measured in REPLAY."},
        })
        if verbose:
            print(f"  [DETERMINISTIC] terminal_state_service_layer: {term_status}")
    finally:
        session.close()

    # -----------------------------------------------------------------------
    # Metrics
    # -----------------------------------------------------------------------
    avg_ms = sum(
        (c["latency_seconds"] or 0) * 1000 for c in case_results
    ) / max(len(case_results), 1)

    # false_positive_rate denominator = clean_cases_evaluated
    fp_rate = round(false_positive_count / clean_cases_evaluated, 4) if clean_cases_evaluated > 0 else None

    deterministic_metrics = {
        "discrepancy_catch_rate": _rate(disc_detected, disc_expected),
        "false_positive_discrepancy_count": false_positive_count,
        "false_positive_rate": fp_rate,
        "false_positive_rate_denominator": f"{clean_cases_evaluated} clean cases evaluated",
        "commercial_rule_enforcement_rate": _rate(
            disc_detected,  # correctly detecting commercial rule violations
            disc_expected,
        ),
        "gate_blocking_rate": _rate(gate_block_correct, gate_block_total),
        "terminal_state_enforcement_rate": _rate(terminal_correct, terminal_total),
    }

    e2e_metrics = {
        "average_reconciliation_duration_ms": round(avg_ms, 2),
        "operator_actions_recorded": 0,
        "terminal_status_counts": {"Approved": 0, "Rejected": 0},
        "visible_replay_mode_badge_rate": 0.0,
    }

    # SC traceability for DETERMINISTIC mode
    sc002_status = "PASS" if disc_detected >= 4 else ("PARTIALLY_MEASURED" if disc_detected > 0 else "FAIL")
    sc_traceability = {
        "SC_001": _sc_partially_measured(
            metric_name="operator_e2e_completion_seconds",
            value=None,
            unit="seconds",
            auto_ms=None,  # Not measured here: ORM construction != E2E path
            notes=(
                "DETERMINISTIC mode does not exercise an end-to-end system path. "
                "SC-001 automated timing is measured in REPLAY mode. "
                "PASS/FAIL requires VLD-EVAL-03 assisted human timing."
            ),
        ),
        "SC_002": _sc_result(
            metric_name="discrepancy_detection_accuracy",
            status=sc002_status,
            value=f"{disc_detected}/{disc_expected}",
            unit="categories_detected",
            notes=(
                f"All 4 canonical discrepancy categories tested: ArithmeticMismatch, "
                f"QuantityOrPackagingBreach, CatalogMatchingMismatch, PriceMismatch. "
                f"Detected {disc_detected} of {disc_expected} expected. "
                f"Stateful app lifecycle (discrepancy_apex) measured in REPLAY mode."
            ),
        ),
        "SC_003": _sc_not_measured(
            "operator_sku_resolution_completion",
            "SC-003 requires stateful operator SKU resolution lifecycle measured in REPLAY mode.",
        ),
        "SC_004": _sc_not_measured(
            "provenance_citation_coverage",
            "SC-004 requires HTTP-level provenance records from REPLAY fixture ingestion path.",
        ),
        "SC_005": _sc_not_measured(
            "approval_gate_enforcement_http",
            "SC-005 DET-4/DET-5 require HTTP 409 verification, measured in REPLAY mode. "
            "Service-layer gate verified separately as gate_blocking_rate.",
        ),
        "SC_006": _sc_not_measured(
            "unextractable_pdf_explicit_error",
            "SC-006 requires HTTP-level file ingestion test, measured in REPLAY mode.",
        ),
    }

    return {
        "case_results": case_results,
        "deterministic_metrics": deterministic_metrics,
        "e2e_metrics": e2e_metrics,
        "sc_traceability": sc_traceability,
    }


# ===========================================================================
# REPLAY mode — current application HTTP/API evaluation
# ===========================================================================

def run_replay(manifest: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    """Current application HTTP/API evaluation using FastAPI TestClient.

    Zero network calls. Uses isolated in-memory DB with seeded baseline data.
    Exercises fixture ingestion, discrepancy lifecycle, operator actions,
    DET-4/DET-5 HTTP gate enforcement, provenance records, is_replay_mode badge.
    """
    case_results: list[dict[str, Any]] = []

    # Create isolated application client
    client, session, engine, app_inst, get_db_fn = _make_eval_client()

    # Dedicated SC-001 timing variable — set exactly once, never overwritten
    sc001_automated_ms: float | None = None

    sc002_data: dict[str, Any] = {"status": "NOT_YET_MEASURED", "detail": {}}
    sc003_data: dict[str, Any] = {"status": "NOT_YET_MEASURED", "detail": {}}
    sc004_data: dict[str, Any] = {"status": "NOT_YET_MEASURED", "detail": {}}
    sc005_data: dict[str, Any] = {"status": "NOT_YET_MEASURED", "detail": {}}
    sc006_data: dict[str, Any] = {"status": "NOT_YET_MEASURED", "detail": {}}
    replay_badge_count = 0
    replay_badge_total = 0
    terminal_approved = 0
    terminal_rejected = 0
    operator_actions = 0

    try:
        # -----------------------------------------------------------------------
        # clean_acme — SC-001 timing, SC-004 provenance, replay badge
        # -----------------------------------------------------------------------
        t0 = time.perf_counter()
        resp = client.post("/api/v1/fixtures/fixture-clean-acme/ingest")
        if resp.status_code == 201:
            draft = resp.json()
            draft_id = draft["draft_id"]
            replay_badge_total += 1
            if draft.get("is_replay_mode") is True:
                replay_badge_count += 1

            # SC-001: measure automated system path (ingest + reconciliation only)
            # This is the machine-measurable portion; human timing is separate.
            sc001_automated_ms = (time.perf_counter() - t0) * 1000

            # Check initial status
            get_resp = client.get(f"/api/v1/drafts/{draft_id}")
            get_draft = get_resp.json() if get_resp.status_code == 200 else {}
            initial_status = get_draft.get("status")

            # SC-004: provenance coverage
            lines = get_draft.get("line_items", [])
            prov_expected = 0
            prov_found = 0
            prov_fields = ("customer_description", "extracted_quantity",
                           "extracted_unit_price", "extracted_line_total")
            for line in lines:
                field_prov = line.get("field_provenance", {}) or {}
                for field in prov_fields:
                    prov_expected += 1
                    pv = field_prov.get(field)
                    if (pv is not None
                            and pv.get("verbatim_snippet") is not None
                            and pv.get("location") is not None):
                        prov_found += 1

            if prov_expected > 0:
                prov_rate = round(100.0 * prov_found / prov_expected, 2)
                sc004_status = "PASS" if prov_found == prov_expected else "FAIL"
                sc004_data = {
                    "status": sc004_status,
                    "detail": {
                        "numerator": prov_found,
                        "denominator": prov_expected,
                        "rate_percentage": prov_rate,
                    },
                }
            else:
                sc004_data = {"status": "MISSING_EVALUATION_ASSET",
                              "detail": {"note": "No line items found in clean_acme response"}}

            # Approve clean_acme (it should be Ready for Approval)
            approve_resp = client.post(
                f"/api/v1/drafts/{draft_id}/approve",
                json={"operator_id": "eval-runner"},
            )
            if approve_resp.status_code == 200:
                terminal_approved += 1
                operator_actions += 1
                clean_status = "PASS" if initial_status == "Ready for Approval" else "FAIL"
            else:
                clean_status = "FAIL"

            case_results.append({
                "case_id": "clean_acme",
                "status": clean_status,
                "latency_seconds": round(sc001_automated_ms / 1000, 4),
                "details": {
                    "initial_status": initial_status,
                    "is_replay_mode": draft.get("is_replay_mode"),
                    "approve_status_code": approve_resp.status_code,
                    "provenance_rate": f"{prov_found}/{prov_expected}",
                },
            })
        else:
            case_results.append({
                "case_id": "clean_acme", "status": "ERROR",
                "latency_seconds": None,
                "details": {"http_status": resp.status_code, "body": resp.text[:200]},
            })
        if verbose:
            print(f"  [REPLAY] clean_acme: {case_results[-1]['status']}")

        # -----------------------------------------------------------------------
        # discrepancy_apex — State A → DET-4 gate → State B → State C
        # -----------------------------------------------------------------------
        t0 = time.perf_counter()
        resp = client.post("/api/v1/fixtures/fixture-discrepancy-apex/ingest")
        if resp.status_code == 201:
            draft = resp.json()
            draft_id = draft["draft_id"]
            replay_badge_total += 1
            if draft.get("is_replay_mode") is True:
                replay_badge_count += 1

            # State A: check discrepancies
            state_a_status = draft.get("status")
            state_a_lines = draft.get("line_items", [])
            all_discrepancy_types = set()
            for line in state_a_lines:
                for disc in line.get("discrepancies", []):
                    if disc.get("resolution_state") == "Unresolved":
                        all_discrepancy_types.add(disc.get("discrepancy_type"))
            # Also check draft-level flags
            for disc in draft.get("discrepancies", []):
                if disc.get("resolution_state") == "Unresolved":
                    all_discrepancy_types.add(disc.get("discrepancy_type"))

            has_price_mismatch = "PriceMismatch" in all_discrepancy_types
            has_catalog_mismatch = "CatalogMatchingMismatch" in all_discrepancy_types
            state_a_ok = state_a_status == "Needs Review" and has_price_mismatch and has_catalog_mismatch

            # DET-4: gate blocking — try to approve a Needs Review draft
            det4_resp = client.post(
                f"/api/v1/drafts/{draft_id}/approve",
                json={"operator_id": "eval-runner"},
            )
            det4_ok = (
                det4_resp.status_code == 409
                and det4_resp.json().get("error") == "DraftNotReadyForApprovalError"
            )
            if det4_ok:
                sc005_data = {
                    "status": "PASS",
                    "detail": {
                        "http_status_code": 409,
                        "error": "DraftNotReadyForApprovalError",
                        "message": det4_resp.json().get("message"),
                        "note": "DET-4 gate confirmed: HTTP 409 on Needs Review draft.",
                    },
                }
            else:
                sc005_data = {
                    "status": "FAIL",
                    "detail": {
                        "http_status_code": det4_resp.status_code,
                        "response": det4_resp.text[:200],
                    },
                }

            # State B: SelectSKU on line 2
            line2 = next((l for l in state_a_lines if l.get("line_number") == 2), None)
            state_b_ok = False
            if line2:
                line2_id = line2["line_id"]
                patch_resp = client.patch(
                    f"/api/v1/drafts/{draft_id}/lines/{line2_id}",
                    json={"action": "SelectSKU", "matched_sku": "SKU-WRAP-15"},
                )
                operator_actions += 1
                if patch_resp.status_code == 200:
                    b_draft = patch_resp.json()
                    b_status = b_draft.get("status")
                    b_lines = b_draft.get("line_items", [])
                    b_discrepancy_types = set()
                    for line in b_lines:
                        for disc in line.get("discrepancies", []):
                            if disc.get("resolution_state") == "Unresolved":
                                b_discrepancy_types.add(disc.get("discrepancy_type"))
                    has_moq_breach = "QuantityOrPackagingBreach" in b_discrepancy_types
                    state_b_ok = (b_status == "Needs Review" and has_moq_breach)
                    sc002_data = {
                        "status": "PASS" if (state_a_ok and state_b_ok) else "FAIL",
                        "detail": {
                            "state_a_has_price_mismatch": has_price_mismatch,
                            "state_a_has_catalog_mismatch": has_catalog_mismatch,
                            "state_b_has_moq_breach": has_moq_breach,
                            "state_b_discrepancies": list(b_discrepancy_types),
                        },
                    }
                else:
                    sc002_data = {"status": "FAIL",
                                  "detail": {"patch_http_status": patch_resp.status_code}}
            else:
                sc002_data = {"status": "FAIL",
                              "detail": {"note": "Line 2 not found in State A response"}}

            # State C: reject
            reject_resp = client.post(
                f"/api/v1/drafts/{draft_id}/reject",
                json={"operator_id": "eval-runner",
                      "reason": "Non-compliant price and MOQ breach"},
            )
            operator_actions += 1
            state_c_ok = reject_resp.status_code == 200
            if state_c_ok:
                c_draft = reject_resp.json()
                terminal_rejected += 1
                state_c_status = c_draft.get("status")
                state_c_ok = state_c_status == "Rejected"

            discrepancy_apex_status = "PASS" if (state_a_ok and state_b_ok and state_c_ok) else "FAIL"
            case_results.append({
                "case_id": "discrepancy_apex",
                "status": discrepancy_apex_status,
                "latency_seconds": round((time.perf_counter() - t0), 4),
                "state_transitions": [
                    {"state_name": "state_a_intake", "status_ok": state_a_ok},
                    {"state_name": "state_b_operator_selects_sku", "status_ok": state_b_ok},
                    {"state_name": "state_c_rejection", "status_ok": state_c_ok},
                ],
                "details": {
                    "state_a_status": state_a_status,
                    "state_a_discrepancies": list(all_discrepancy_types),
                    "det4_gate_ok": det4_ok,
                    "sc002_result": sc002_data.get("status"),
                },
            })
        else:
            case_results.append({
                "case_id": "discrepancy_apex", "status": "ERROR",
                "latency_seconds": None,
                "details": {"http_status": resp.status_code},
            })
        if verbose:
            print(f"  [REPLAY] discrepancy_apex: {case_results[-1]['status']}")

        # -----------------------------------------------------------------------
        # ambiguous_apex — State A → B → C, then DET-5 terminal state
        # -----------------------------------------------------------------------
        t0 = time.perf_counter()
        resp = client.post("/api/v1/fixtures/fixture-ambiguous-apex/ingest")
        if resp.status_code == 201:
            draft = resp.json()
            draft_id = draft["draft_id"]
            replay_badge_total += 1
            if draft.get("is_replay_mode") is True:
                replay_badge_count += 1

            # State A: CatalogMatchingMismatch
            state_a_status = draft.get("status")
            state_a_lines = draft.get("line_items", [])
            a_discrepancy_types = set()
            for line in state_a_lines:
                for disc in line.get("discrepancies", []):
                    if disc.get("resolution_state") == "Unresolved":
                        a_discrepancy_types.add(disc.get("discrepancy_type"))
            state_a_ok = (state_a_status == "Needs Review"
                          and "CatalogMatchingMismatch" in a_discrepancy_types)

            # State B: SelectSKU SKU-WRAP-15
            line1 = next((l for l in state_a_lines if l.get("line_number") == 1), None)
            state_b_ok = False
            if line1:
                line1_id = line1["line_id"]
                patch_resp = client.patch(
                    f"/api/v1/drafts/{draft_id}/lines/{line1_id}",
                    json={"action": "SelectSKU", "matched_sku": "SKU-WRAP-15"},
                )
                operator_actions += 1
                if patch_resp.status_code == 200:
                    b_draft = patch_resp.json()
                    b_status = b_draft.get("status")
                    state_b_ok = (b_status == "Ready for Approval")
                    sc003_data = {
                        "status": "PASS" if state_b_ok else "FAIL",
                        "detail": {
                            "after_sku_selection_status": b_status,
                            "state_a_had_catalog_mismatch": "CatalogMatchingMismatch" in a_discrepancy_types,
                        },
                    }
                else:
                    sc003_data = {"status": "FAIL",
                                  "detail": {"patch_http_status": patch_resp.status_code}}
            else:
                sc003_data = {"status": "FAIL",
                              "detail": {"note": "Line 1 not found in ambiguous_apex State A"}}

            # State C: approve
            approve_resp = client.post(
                f"/api/v1/drafts/{draft_id}/approve",
                json={"operator_id": "eval-runner"},
            )
            operator_actions += 1
            state_c_ok = approve_resp.status_code == 200
            if state_c_ok:
                terminal_approved += 1

            # DET-5: terminal-state re-transition — try to approve the same approved draft
            if state_c_ok:
                det5_resp = client.post(
                    f"/api/v1/drafts/{draft_id}/approve",
                    json={"operator_id": "eval-runner"},
                )
                det5_ok = (
                    det5_resp.status_code == 409
                    and det5_resp.json().get("error") == "TerminalDraftConflictError"
                )
                if det5_ok and sc005_data.get("status") == "PASS":
                    # Both DET-4 and DET-5 passed — update notes
                    sc005_data["detail"]["det5_terminal_state_ok"] = True
                    sc005_data["detail"]["det5_http_status"] = 409
                    sc005_data["detail"]["det5_error"] = "TerminalDraftConflictError"
                elif det5_ok and sc005_data.get("status") != "PASS":
                    sc005_data = {
                        "status": "PARTIALLY_MEASURED",
                        "detail": {
                            "det4_ok": False,
                            "det5_ok": True,
                            "note": "DET-5 passed, DET-4 failed",
                        },
                    }
                elif not det5_ok:
                    # DET-5 failed — downgrade SC-005 if it was PASS
                    sc005_data["detail"]["det5_ok"] = False
                    sc005_data["detail"]["det5_http_status"] = det5_resp.status_code
                    if sc005_data.get("status") == "PASS":
                        sc005_data["status"] = "PARTIALLY_MEASURED"

                case_results.append({
                    "case_id": "det5_terminal_state_http",
                    "status": "PASS" if det5_ok else "FAIL",
                    "latency_seconds": None,
                    "details": {
                        "http_status_code": det5_resp.status_code,
                        "error": det5_resp.json().get("error") if det5_ok else None,
                        "message": "Cannot modify an Approved or Rejected draft",
                    },
                })

            ambiguous_status = "PASS" if (state_a_ok and state_b_ok and state_c_ok) else "FAIL"
            case_results.append({
                "case_id": "ambiguous_apex",
                "status": ambiguous_status,
                "latency_seconds": round((time.perf_counter() - t0), 4),
                "state_transitions": [
                    {"state_name": "state_a_intake", "status_ok": state_a_ok},
                    {"state_name": "state_b_operator_selects_sku", "status_ok": state_b_ok},
                    {"state_name": "state_c_approval", "status_ok": state_c_ok},
                ],
                "details": {
                    "state_a_status": state_a_status,
                    "sc003_result": sc003_data.get("status"),
                },
            })
        else:
            case_results.append({
                "case_id": "ambiguous_apex", "status": "ERROR",
                "latency_seconds": None,
                "details": {"http_status": resp.status_code},
            })
        if verbose:
            print(f"  [REPLAY] ambiguous_apex: {case_results[-1]['status']}")

        # -----------------------------------------------------------------------
        # SC-006: unextractable PDF via POST /api/v1/orders/ingest
        # We override the AI provider dependency so the PDF parser runs but AI never fires.
        # -----------------------------------------------------------------------
        unextractable_path = _REPO_ROOT / "tests" / "fixtures" / "po_unextractable.pdf"
        if unextractable_path.exists():
            try:
                from app.services.ai_provider import OrderShieldAIProvider
                from app.api.routes_orders import get_live_ai_provider

                class _NeverCalledProvider(OrderShieldAIProvider):
                    def extract(self, *args, **kwargs):
                        raise AssertionError("AI provider must not be called for unextractable PDF")

                app_inst.dependency_overrides[get_live_ai_provider] = lambda: _NeverCalledProvider()
                try:
                    pdf_bytes = unextractable_path.read_bytes()
                    pdf_resp = client.post(
                        "/api/v1/orders/ingest",
                        files={"file": (unextractable_path.name, pdf_bytes, "application/pdf")},
                    )
                    if pdf_resp.status_code == 400:
                        body = pdf_resp.json()
                        if body.get("error") == "UnextractableTextError":
                            sc006_status = "PASS"
                            sc006_note = "HTTP 400 UnextractableTextError confirmed."
                        else:
                            sc006_status = "FAIL"
                            sc006_note = f"HTTP 400 but wrong error: {body.get('error')}"
                    else:
                        sc006_status = "FAIL"
                        sc006_note = f"Expected HTTP 400, got {pdf_resp.status_code}"
                    sc006_data = {"status": sc006_status, "detail": {
                        "http_status_code": pdf_resp.status_code,
                        "error": pdf_resp.json().get("error") if pdf_resp.status_code == 400 else None,
                        "note": sc006_note,
                    }}
                    case_results.append({
                        "case_id": "unextractable_pdf",
                        "status": sc006_status,
                        "latency_seconds": None,
                        "details": sc006_data["detail"],
                    })
                finally:
                    app_inst.dependency_overrides.pop(get_live_ai_provider, None)
            except Exception as exc:
                sc006_data = {"status": "MISSING_EVALUATION_ASSET",
                              "detail": {"error": str(exc)}}
                case_results.append({
                    "case_id": "unextractable_pdf", "status": "ERROR",
                    "latency_seconds": None, "details": {"error": str(exc)},
                })
        else:
            sc006_data = {"status": "MISSING_EVALUATION_ASSET",
                          "detail": {"note": f"Fixture not found: {unextractable_path}"}}
        if verbose:
            print(f"  [REPLAY] unextractable_pdf: {sc006_data.get('status')}")

    finally:
        _cleanup_eval_client(app_inst, get_db_fn, session, engine)

    # -----------------------------------------------------------------------
    # Aggregate metrics
    # -----------------------------------------------------------------------
    replay_badge_rate = (
        round(replay_badge_count / replay_badge_total, 4)
        if replay_badge_total > 0 else 0.0
    )

    deterministic_metrics = {
        "discrepancy_catch_rate": _rate(0, 0),
        "false_positive_discrepancy_count": 0,
        "commercial_rule_enforcement_rate": _rate(0, 0),
        "gate_blocking_rate": _rate(
            1 if sc005_data.get("status") == "PASS" else 0,
            1,
        ),
        "terminal_state_enforcement_rate": _rate(0, 0),
    }

    e2e_metrics = {
        "average_reconciliation_duration_ms": round(
            sc001_automated_ms or 0.0, 2
        ),
        "operator_actions_recorded": operator_actions,
        "terminal_status_counts": {"Approved": terminal_approved, "Rejected": terminal_rejected},
        "visible_replay_mode_badge_rate": replay_badge_rate,
    }

    # SC-004 result
    sc004_entry: dict[str, Any]
    if sc004_data.get("status") == "PASS":
        pov_d = sc004_data["detail"]
        sc004_entry = _sc_result(
            "provenance_citation_coverage",
            status="PASS",
            value=pov_d.get("rate_percentage"),
            unit="percent",
            notes=f"Provenance fields verified: {pov_d.get('numerator')}/{pov_d.get('denominator')} "
                  "expected fields have verbatim_snippet and location. "
                  "Evaluated using clean_acme fixture through /api/v1/fixtures/fixture-clean-acme/ingest.",
        )
    elif sc004_data.get("status") == "FAIL":
        pov_d = sc004_data["detail"]
        sc004_entry = _sc_result(
            "provenance_citation_coverage",
            status="FAIL",
            value=pov_d.get("rate_percentage"),
            unit="percent",
            notes=f"Provenance check: {pov_d.get('numerator')}/{pov_d.get('denominator')} fields valid.",
        )
    else:
        sc004_entry = _sc_missing("provenance_citation_coverage", sc004_data.get("detail", {}).get("note", ""))

    # SC-005 result
    sc005_entry: dict[str, Any]
    if sc005_data.get("status") == "PASS":
        sc005_entry = _sc_result(
            "approval_gate_enforcement_http",
            status="PASS",
            value="HTTP 409 DraftNotReadyForApprovalError",
            unit=None,
            notes=str(sc005_data.get("detail", {})),
        )
    elif sc005_data.get("status") == "PARTIALLY_MEASURED":
        sc005_entry = _sc_result(
            "approval_gate_enforcement_http",
            status="PARTIALLY_MEASURED",
            value=None, unit=None,
            notes=str(sc005_data.get("detail", {})),
        )
    else:
        sc005_entry = _sc_result(
            "approval_gate_enforcement_http",
            status="FAIL" if sc005_data.get("status") == "FAIL" else "NOT_YET_MEASURED",
            value=None, unit=None,
            notes=str(sc005_data.get("detail", {})),
        )

    # SC-006 result
    sc006_entry: dict[str, Any]
    s6 = sc006_data.get("status", "NOT_YET_MEASURED")
    if s6 in ("PASS", "FAIL", "PARTIALLY_MEASURED"):
        sc006_entry = _sc_result(
            "unextractable_pdf_explicit_error",
            status=s6, value=None, unit=None,
            notes=str(sc006_data.get("detail", {})),
        )
    else:
        sc006_entry = _sc_missing("unextractable_pdf_explicit_error",
                                  str(sc006_data.get("detail", {}).get("note", "")))

    sc002_entry: dict[str, Any]
    s2 = sc002_data.get("status", "NOT_YET_MEASURED")
    if s2 in ("PASS", "FAIL"):
        sc002_d = sc002_data.get("detail", {})
        sc002_entry = _sc_result(
            "discrepancy_detection_accuracy",
            status=s2,
            value=None, unit=None,
            notes=(
                f"Current-app E2E: discrepancy_apex State A detected "
                f"PriceMismatch={sc002_d.get('state_a_has_price_mismatch')}, "
                f"CatalogMatchingMismatch={sc002_d.get('state_a_has_catalog_mismatch')}; "
                f"State B detected QuantityOrPackagingBreach={sc002_d.get('state_b_has_moq_breach')}."
            ),
        )
    else:
        sc002_entry = _sc_not_measured("discrepancy_detection_accuracy",
                                       "discrepancy_apex fixture evaluation did not complete.")

    sc003_entry: dict[str, Any]
    s3 = sc003_data.get("status", "NOT_YET_MEASURED")
    if s3 in ("PASS", "FAIL"):
        sc003_d = sc003_data.get("detail", {})
        sc003_entry = _sc_result(
            "operator_sku_resolution_completion",
            status=s3, value=None, unit=None,
            notes=(
                f"ambiguous_apex: after SelectSKU status={sc003_d.get('after_sku_selection_status')}. "
                f"State A CatalogMatchingMismatch={sc003_d.get('state_a_had_catalog_mismatch')}."
            ),
        )
    else:
        sc003_entry = _sc_not_measured("operator_sku_resolution_completion",
                                       "ambiguous_apex fixture evaluation did not complete.")

    sc_traceability = {
        "SC_001": _sc_partially_measured(
            metric_name="operator_e2e_completion_seconds",
            value=round(sc001_automated_ms, 2) if sc001_automated_ms is not None else None,
            unit="milliseconds",
            auto_ms=round(sc001_automated_ms, 2) if sc001_automated_ms is not None else None,
            notes=(
                "automated_system_path_duration: fixture-clean-acme ingest → reconciliation complete. "
                "This is the machine-measurable portion of SC-001. "
                "PASS/FAIL requires VLD-EVAL-03 assisted human timing."
            ),
        ),
        "SC_002": sc002_entry,
        "SC_003": sc003_entry,
        "SC_004": sc004_entry,
        "SC_005": sc005_entry,
        "SC_006": sc006_entry,
    }

    return {
        "case_results": case_results,
        "deterministic_metrics": deterministic_metrics,
        "e2e_metrics": e2e_metrics,
        "sc_traceability": sc_traceability,
    }


# ===========================================================================
# HISTORICAL_BAKEOFF mode
# ===========================================================================

def _find_primary_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the canonical primary (Alibaba Qwen) by explicit identity, never candidates[0].

    ADR 0001: primary = Alibaba Model Studio / Qwen.
    """
    for candidate in candidates:
        provider = (candidate.get("provider") or "").lower()
        model = (candidate.get("requested_model") or "").lower()
        if "alibaba" in provider or "qwen" in provider or "qwen" in model:
            return candidate
    return None


def _find_secondary_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the secondary candidate (Gemini) by explicit identity."""
    for candidate in candidates:
        provider = (candidate.get("provider") or "").lower()
        model = (candidate.get("requested_model") or "").lower()
        if "gemini" in provider or "google" in provider or "gemini" in model:
            return candidate
    return None


def run_historical_bakeoff(manifest: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    """Historical spikes/ordershield/** evidence evaluation.

    Uses frozen bake-off schema vocabulary. NEVER co-mingles with current-app metrics.
    Primary provider: Alibaba Model Studio / Qwen (ADR 0001) — not candidates[0].
    SC-002/SC-003: NOT_YET_MEASURED (historical != current-app E2E behavior).
    """
    if not _PHASE2_SUMMARY.exists():
        return _empty_result_with_note(
            "MISSING_EVALUATION_ASSET",
            f"Phase2 summary not found: {_PHASE2_SUMMARY}",
        )

    phase2 = json.loads(_PHASE2_SUMMARY.read_text(encoding="utf-8"))
    candidates = phase2.get("candidates", [])
    if not candidates:
        return _empty_result_with_note("MISSING_EVALUATION_ASSET", "No candidates in phase2_summary.json")

    expected = json.loads(_EXPECTED_JSON.read_text(encoding="utf-8")) if _EXPECTED_JSON.exists() else {}

    # --- Select primary by explicit identity (ADR 0001) ---
    primary = _find_primary_candidate(candidates)
    if primary is None:
        return _empty_result_with_note(
            "MISSING_EVALUATION_ASSET",
            "Could not find Alibaba/Qwen primary candidate by provider/model identity in phase2_summary.json. "
            "ADR 0001 requires Alibaba Model Studio / Qwen as primary.",
        )

    secondary = _find_secondary_candidate(candidates)

    primary_provider_name = primary.get("provider", "unknown")
    primary_model_name = primary.get("requested_model", "unknown")

    case_results: list[dict[str, Any]] = []

    # Evaluate primary candidate per-fixture
    primary_p1 = primary.get("phase1_selected", {})
    per_fixture = primary_p1.get("per_fixture", {})

    for fixture_id, fixture_data in per_fixture.items():
        exp = expected.get(fixture_id, {})
        exp_status = exp.get("expected_status")
        observations = fixture_data.get("observations", [])
        all_error_classes = fixture_data.get("all_error_classes", {})
        outcomes = fixture_data.get("outcomes", {})

        # Determine actual core_status from observations
        actual_status = None
        for obs in observations:
            cs = obs.get("core_status")
            if cs is not None:
                actual_status = cs
                break

        # PASS: core_status matches expected AND no error classes
        # FAIL: either condition fails — do not hide/reinterpret historical failures
        if actual_status is None and "LOCAL_VALIDATION_FAILURE" in (all_error_classes or {}):
            # h10_damaged: LOCAL_VALIDATION_FAILURE with null core_status
            # This is a FAIL against frozen expected outcome HUMAN_REVIEW
            status = "FAIL"
            note = "LOCAL_VALIDATION_FAILURE present. Reproduction noted in details."
        elif actual_status == exp_status and not all_error_classes:
            status = "PASS"
            note = f"core_status={actual_status} matches expected, no errors."
        elif actual_status == exp_status and all_error_classes:
            # Status matches but errors exist — FAIL (e.g. h08 SEMANTIC_WRONG)
            status = "FAIL"
            note = f"core_status={actual_status} matches expected but error_classes={list(all_error_classes.keys())} present."
        else:
            status = "FAIL"
            note = f"core_status={actual_status} expected={exp_status}."

        case_results.append({
            "case_id": f"{fixture_id}::{primary_provider_name[:30]}",
            "status": status,
            "latency_seconds": round(
                fixture_data.get("latency_ms", {}).get("mean_ms", 0) / 1000, 4
            ),
            "details": {
                "provider": primary_provider_name,
                "model": primary_model_name,
                "actual_core_status": actual_status,
                "expected_core_status": exp_status,
                "all_error_classes": all_error_classes,
                "outcomes": outcomes,
                "note": note,
            },
        })
        if verbose:
            print(f"  [HISTORICAL_BAKEOFF] {fixture_id} ({primary_provider_name[:20]}): {status}")

    # Also evaluate secondary (Gemini) for completeness
    if secondary:
        sec_p1 = secondary.get("phase1_selected", {})
        sec_per_fixture = sec_p1.get("per_fixture", {})
        sec_provider = secondary.get("provider", "unknown")
        sec_model = secondary.get("requested_model", "unknown")
        for fixture_id, fixture_data in sec_per_fixture.items():
            exp = expected.get(fixture_id, {})
            exp_status = exp.get("expected_status")
            all_error_classes = fixture_data.get("all_error_classes", {})
            outcomes = fixture_data.get("outcomes", {})
            observations = fixture_data.get("observations", [])
            actual_status = None
            for obs in observations:
                cs = obs.get("core_status")
                if cs is not None:
                    actual_status = cs
                    break

            if actual_status is None and "LOCAL_VALIDATION_FAILURE" in (all_error_classes or {}):
                status = "FAIL"
            elif actual_status == exp_status and not all_error_classes:
                status = "PASS"
            else:
                status = "FAIL"

            case_results.append({
                "case_id": f"{fixture_id}::{sec_provider[:30]}[secondary]",
                "status": status,
                "latency_seconds": round(
                    fixture_data.get("latency_ms", {}).get("mean_ms", 0) / 1000, 4
                ),
                "details": {
                    "provider": sec_provider,
                    "model": sec_model,
                    "secondary_evidence_only": True,
                    "actual_core_status": actual_status,
                    "expected_core_status": exp_status,
                    "all_error_classes": all_error_classes,
                    "outcomes": outcomes,
                },
            })

    # Build historical_bakeoff_metrics from primary (Qwen/Alibaba)
    schema_valid = primary_p1.get("schema_valid_rate", {})
    mandatory = primary_p1.get("exact_mandatory_field_rate", {})
    determinate = primary_p1.get("determinate_sku_rate", {})
    review_trap = primary_p1.get("review_trap_rate", {})
    wrong_sku = primary_p1.get("wrong_confident_sku_count", 0)
    latency = primary_p1.get("latency_ms", {})
    total_calls = primary_p1.get("completed_calls", 1)

    historical_bakeoff_metrics = {
        "provider": primary_provider_name,
        "model": primary_model_name,
        "schema_valid_rate": _rate(
            schema_valid.get("numerator", 0), schema_valid.get("denominator", 1),
        ),
        "mandatory_field_exactness": {
            "numerator": mandatory.get("numerator", 0),
            "denominator": mandatory.get("denominator", 1),
            "rate_percentage": round(100.0 * (mandatory.get("rate") or 0), 4),
        },
        "determinate_sku_accuracy": {
            "numerator": determinate.get("numerator", 0),
            "denominator": max(determinate.get("denominator", 0), 0),
            "rate_percentage": round(100.0 * (determinate.get("rate") or 0), 4),
            "abstentions": review_trap.get("denominator", 0),
        },
        "review_trap_catch_rate": _rate(
            review_trap.get("numerator", 0),
            max(review_trap.get("denominator", 0), 1),
        ),
        "wrong_confident_skus": {
            "count": wrong_sku,
            "rate_percentage": round(100.0 * wrong_sku / max(total_calls, 1), 4),
        },
        "latency_profile_seconds": {
            "min": latency.get("min_ms", 0) / 1000,
            "p50": latency.get("p50_ms", 0) / 1000,
            "mean": latency.get("mean_ms", 0) / 1000,
            "p90": latency.get("p90_ms", 0) / 1000,
            "p95": latency.get("p95_ms", 0) / 1000,
            "max": latency.get("max_ms", 0) / 1000,
        },
    }

    deterministic_metrics = {
        "discrepancy_catch_rate": _rate(0, 0),
        "false_positive_discrepancy_count": 0,
        "commercial_rule_enforcement_rate": _rate(0, 0),
        "gate_blocking_rate": _rate(0, 0),
        "terminal_state_enforcement_rate": _rate(0, 0),
    }

    e2e_metrics = {
        "average_reconciliation_duration_ms": round(latency.get("mean_ms", 0), 2),
        "operator_actions_recorded": 0,
        "terminal_status_counts": {"Approved": 0, "Rejected": 0},
        "visible_replay_mode_badge_rate": 0.0,
    }

    sc_traceability = {
        "SC_001": _sc_partially_measured(
            metric_name="operator_e2e_completion_seconds",
            value=None, unit="seconds", auto_ms=None,
            notes="HISTORICAL_BAKEOFF does not exercise current-app SC-001 path.",
        ),
        "SC_002": _sc_not_measured(
            "discrepancy_detection_accuracy",
            "HISTORICAL_BAKEOFF: review_trap_catch_rate is historical AI evidence, NOT SC-002. "
            "SC-002 is discrepancy_detection_accuracy measured using current-app E2E in REPLAY mode.",
        ),
        "SC_003": _sc_not_measured(
            "operator_sku_resolution_completion",
            "HISTORICAL_BAKEOFF: historical bake-off review routing is not equivalent to current-app "
            "operator SKU resolution lifecycle. SC-003 measured in REPLAY mode.",
        ),
        "SC_004": _sc_not_measured(
            "provenance_citation_coverage",
            "HISTORICAL_BAKEOFF does not exercise current-app provenance path. SC-004 in REPLAY.",
        ),
        "SC_005": _sc_not_measured(
            "approval_gate_enforcement_http",
            "HISTORICAL_BAKEOFF does not exercise current-app HTTP approval gate. SC-005 in REPLAY.",
        ),
        "SC_006": _sc_not_measured(
            "unextractable_pdf_explicit_error",
            "HISTORICAL_BAKEOFF validates historical text fixtures. SC-006 measured in REPLAY.",
        ),
    }

    return {
        "case_results": case_results,
        "deterministic_metrics": deterministic_metrics,
        "e2e_metrics": e2e_metrics,
        "sc_traceability": sc_traceability,
        "ai_metrics": {
            "historical_bakeoff_metrics": historical_bakeoff_metrics,
            # app_ai_metrics intentionally absent — schema separation invariant
        },
    }


# ===========================================================================
# LIVE mode (gated)
# ===========================================================================

def run_live(manifest: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    """Live provider evaluation — gated by LIVE_EVALUATION_ENABLED=true.

    When enabled: submits clean_acme PO text to POST /api/v1/orders/ingest
    using the configured live AI provider. Records provider/model provenance.
    When not enabled: returns MISSING_EVALUATION_ASSET. Never raises.
    """
    if os.environ.get("LIVE_EVALUATION_ENABLED", "").lower() != "true":
        print(
            "LIVE mode requires LIVE_EVALUATION_ENABLED=true. "
            "Not running to protect provider quota.",
            flush=True,
        )
        return _empty_result_with_note(
            "MISSING_EVALUATION_ASSET",
            "LIVE evaluation not executed: set LIVE_EVALUATION_ENABLED=true to run.",
        )

    # Gated live implementation — exercises POST /api/v1/orders/ingest
    try:
        from sqlalchemy import create_engine
        from sqlalchemy import event as sa_event
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from fastapi.testclient import TestClient
        from app.main import app
        from app.database import Base, get_db
        from app.cli import seed_baseline
        import app.models.entities  # noqa: F401

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @sa_event.listens_for(engine, "connect")
        def _fk(conn, _rec):
            conn.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(bind=engine)
        factory = sessionmaker(bind=engine)
        session = factory()
        seed_baseline(session)
        session.commit()

        def _get_db_override():
            yield session

        app.dependency_overrides[get_db] = _get_db_override
        client = TestClient(app, raise_server_exceptions=False)

        try:
            clean_acme_path = _REPO_ROOT / "tests" / "fixtures" / "po_clean_acme.txt"
            t0 = time.perf_counter()
            resp = client.post(
                "/api/v1/orders/ingest",
                files={"file": (clean_acme_path.name, clean_acme_path.read_bytes(), "text/plain")},
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000

            case_results = [{
                "case_id": "live_clean_acme_ingest",
                "status": "PASS" if resp.status_code == 201 else "FAIL",
                "latency_seconds": round(elapsed_ms / 1000, 4),
                "details": {"http_status_code": resp.status_code},
            }]

            from app.config import settings
            provider_name = getattr(settings, "ai_provider", "unknown")
            model_name = getattr(settings, "ai_model", "unknown")

        finally:
            app.dependency_overrides.pop(get_db, None)
            session.close()
            Base.metadata.drop_all(bind=engine)
            engine.dispose()

        return {
            "case_results": case_results,
            "provider_config": {
                "provider": provider_name,
                "model": model_name,
                "base_url_masked": None,
                "timeout_seconds": None,
            },
            "deterministic_metrics": {
                "discrepancy_catch_rate": _rate(0, 0),
                "false_positive_discrepancy_count": 0,
                "commercial_rule_enforcement_rate": _rate(0, 0),
                "gate_blocking_rate": _rate(0, 0),
                "terminal_state_enforcement_rate": _rate(0, 0),
            },
            "e2e_metrics": {
                "average_reconciliation_duration_ms": round(elapsed_ms, 2),
                "operator_actions_recorded": 0,
                "terminal_status_counts": {"Approved": 0, "Rejected": 0},
                "visible_replay_mode_badge_rate": 0.0,
            },
            "sc_traceability": {
                "SC_001": _sc_partially_measured(
                    "operator_e2e_completion_seconds", round(elapsed_ms, 2), "milliseconds",
                    f"LIVE ingest of clean_acme via {provider_name}/{model_name}. "
                    "PASS/FAIL requires VLD-EVAL-03 human timing.",
                    auto_ms=round(elapsed_ms, 2),
                ),
                "SC_002": _sc_not_measured("discrepancy_detection_accuracy", "LIVE measures AI extraction, not discrepancy detection."),
                "SC_003": _sc_not_measured("operator_sku_resolution_completion", "LIVE measures AI extraction."),
                "SC_004": _sc_not_measured("provenance_citation_coverage", "LIVE provenance not evaluated here."),
                "SC_005": _sc_not_measured("approval_gate_enforcement_http", "Not measured in LIVE mode."),
                "SC_006": _sc_not_measured("unextractable_pdf_explicit_error", "Not measured in LIVE mode."),
            },
            "ai_metrics": {"app_ai_metrics": {"latency_profile_seconds": {"mean": round(elapsed_ms / 1000, 4)}}},
        }

    except Exception as exc:
        return _empty_result_with_note("MISSING_EVALUATION_ASSET", f"LIVE evaluation error: {exc}")


# ===========================================================================
# ALL mode
# ===========================================================================

def run_all(manifest: dict[str, Any], verbose: bool = False) -> dict[str, Any]:
    """Run DETERMINISTIC + REPLAY + HISTORICAL_BAKEOFF. LIVE only if explicitly enabled."""
    print("Running DETERMINISTIC ...", flush=True)
    det = run_deterministic(manifest, verbose=verbose)
    print("Running REPLAY ...", flush=True)
    rep = run_replay(manifest, verbose=verbose)
    print("Running HISTORICAL_BAKEOFF ...", flush=True)
    hb = run_historical_bakeoff(manifest, verbose=verbose)

    all_cases = (
        [dict(c, mode="DETERMINISTIC") for c in det.get("case_results", [])]
        + [dict(c, mode="REPLAY") for c in rep.get("case_results", [])]
        + [dict(c, mode="HISTORICAL_BAKEOFF") for c in hb.get("case_results", [])]
    )

    live_payload: dict[str, Any] | None = None
    if os.environ.get("LIVE_EVALUATION_ENABLED", "").lower() == "true":
        print("Running LIVE ...", flush=True)
        live_payload = run_live(manifest, verbose=verbose)
        if live_payload:
            all_cases += [dict(c, mode="LIVE") for c in live_payload.get("case_results", [])]

    # Merge SC traceability: FAIL (rank 3) > PASS (2) > PARTIALLY_MEASURED (1) > NOT_YET/MISSING (0)
    sc_sources = [det["sc_traceability"], rep["sc_traceability"], hb["sc_traceability"]]
    if live_payload:
        sc_sources.append(live_payload["sc_traceability"])
    combined_sc = _merge_sc_traceability(sc_sources)

    # Deterministic metrics from DETERMINISTIC run (most complete)
    combined_det = det["deterministic_metrics"]
    # Override gate_blocking_rate from REPLAY (HTTP-level is more authoritative)
    combined_det["gate_blocking_rate"] = rep["deterministic_metrics"]["gate_blocking_rate"]

    # E2E from REPLAY (most complete)
    combined_e2e = rep["e2e_metrics"]

    # AI metrics: keep schemas separate, never co-mingle
    combined_ai: dict[str, Any] = {}
    for sub in [det, rep, hb]:
        for k, v in sub.get("ai_metrics", {}).items():
            if k not in combined_ai:
                combined_ai[k] = v
    if live_payload:
        for k, v in live_payload.get("ai_metrics", {}).items():
            if k not in combined_ai:
                combined_ai[k] = v

    result = {
        "case_results": all_cases,
        "deterministic_metrics": combined_det,
        "e2e_metrics": combined_e2e,
        "sc_traceability": combined_sc,
    }
    if combined_ai:
        result["ai_metrics"] = combined_ai
    return result


def _merge_sc_traceability(sc_lists: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge SC traceability from multiple runs. FAIL dominates PASS."""
    merged: dict[str, Any] = {}
    for sc_dict in sc_lists:
        for key, entry in sc_dict.items():
            if key not in merged:
                merged[key] = entry
            else:
                current_rank = _SC_STATUS_RANK.get(merged[key].get("status", "NOT_YET_MEASURED"), 0)
                new_rank = _SC_STATUS_RANK.get(entry.get("status", "NOT_YET_MEASURED"), 0)
                if new_rank > current_rank:
                    merged[key] = entry
    return merged


# ===========================================================================
# Error result helper
# ===========================================================================

def _empty_result_with_note(sc_status: str, note: str) -> dict[str, Any]:
    sc_entry: dict[str, Any] = {
        "status": sc_status,
        "metric_name": "unavailable",
        "value": None,
        "unit": None,
        "notes": note,
    }
    return {
        "case_results": [],
        "deterministic_metrics": {
            "discrepancy_catch_rate": _rate(0, 0),
            "false_positive_discrepancy_count": 0,
            "commercial_rule_enforcement_rate": _rate(0, 0),
            "gate_blocking_rate": _rate(0, 0),
            "terminal_state_enforcement_rate": _rate(0, 0),
        },
        "e2e_metrics": {
            "average_reconciliation_duration_ms": 0.0,
            "operator_actions_recorded": 0,
            "terminal_status_counts": {"Approved": 0, "Rejected": 0},
            "visible_replay_mode_badge_rate": 0.0,
        },
        "sc_traceability": {
            "SC_001": {**sc_entry, "automated_system_path_duration_ms": None,
                       "assisted_operator_completion_duration_seconds": None},
            "SC_002": sc_entry,
            "SC_003": sc_entry,
            "SC_004": sc_entry,
            "SC_005": sc_entry,
            "SC_006": sc_entry,
        },
    }


# ===========================================================================
# Main entry point
# ===========================================================================

def run_evaluation(mode: str, verbose: bool = False) -> int:
    """Execute the evaluation runner for the given mode. Returns 0 on success."""
    mode = mode.upper()
    if mode not in VALID_MODES:
        print(f"ERROR: Unknown mode {mode!r}. Valid: {sorted(VALID_MODES)}", flush=True)
        return 1

    print(f"OrderShield Evaluation Runner — mode={mode}", flush=True)

    if not _MANIFEST_PATH.exists():
        print(f"ERROR: Manifest not found: {_MANIFEST_PATH}", flush=True)
        return 1

    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    git_commit = _current_git_commit()
    run_id = str(uuid.uuid4())
    timestamp_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    print(f"Run ID:     {run_id}", flush=True)
    print(f"Git commit: {git_commit}", flush=True)
    print(f"Timestamp:  {timestamp_utc}", flush=True)

    if mode == "DETERMINISTIC":
        payload = run_deterministic(manifest, verbose=verbose)
    elif mode == "REPLAY":
        payload = run_replay(manifest, verbose=verbose)
    elif mode == "HISTORICAL_BAKEOFF":
        payload = run_historical_bakeoff(manifest, verbose=verbose)
    elif mode == "LIVE":
        payload = run_live(manifest, verbose=verbose)
    elif mode == "ALL":
        payload = run_all(manifest, verbose=verbose)
    else:
        print(f"ERROR: Unhandled mode {mode!r}", flush=True)
        return 1

    result: dict[str, Any] = {
        "run_id": run_id,
        "mode": mode,
        "timestamp_utc": timestamp_utc,
        "git_commit": git_commit,
        "deterministic_metrics": payload["deterministic_metrics"],
        "e2e_metrics": payload["e2e_metrics"],
        "sc_traceability": payload["sc_traceability"],
    }
    if "case_results" in payload:
        result["case_results"] = payload["case_results"]
    if "ai_metrics" in payload:
        result["ai_metrics"] = payload["ai_metrics"]
    if "provider_config" in payload:
        result["provider_config"] = payload["provider_config"]

    errors = _validate_against_schema(result)
    if errors:
        print("ERROR: Result failed schema validation:", flush=True)
        for e in errors:
            print(f"  - {e}", flush=True)
        return 1

    ts_path, latest_path = _write_result(result, mode)
    print(f"\nResult written: {ts_path}", flush=True)
    print(f"Latest:         {latest_path}", flush=True)

    sc = result["sc_traceability"]
    print("\n--- SC Traceability Summary ---", flush=True)
    for sc_key in ["SC_001", "SC_002", "SC_003", "SC_004", "SC_005", "SC_006"]:
        entry = sc.get(sc_key, {})
        status = entry.get("status", "?")
        value = entry.get("value")
        val_str = f" ({value})" if value is not None else ""
        print(f"  {sc_key}: {status}{val_str}", flush=True)

    cases = result.get("case_results", [])
    if cases:
        pass_n = sum(1 for c in cases if c.get("status") == "PASS")
        fail_n = sum(1 for c in cases if c.get("status") == "FAIL")
        skip_n = sum(1 for c in cases if c.get("status") == "SKIPPED")
        err_n = sum(1 for c in cases if c.get("status") == "ERROR")
        print(f"\n--- Case Results: {pass_n} PASS / {fail_n} FAIL / {skip_n} SKIPPED / {err_n} ERROR ---",
              flush=True)

    print("\nEvaluation complete.", flush=True)
    return 0
