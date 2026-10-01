"""Unit tests for P2 grounded field corrections, discrepancy resolution, and line removal (T029).

Covers all minimum required scenarios:
A. valid TXT grounding
B. forged TXT location -> SourceGroundingMismatchError
C. grounded snippet but unsupported requested value fails
D. atomic grounding failure leaves durable state unmutated
E. valid money correction
F. genuine commercial discrepancy remains Unresolved
G. obsolete discrepancy -> ResolvedByCorrection
H. line removal -> Removed + ResolvedByLineRemoval
I. subtotal recalculation excludes removed line
J. unrelated blocker on remaining active line stays effective
K. Approved terminal correction/removal blocked
L. Rejected terminal correction/removal blocked
M. positive PDF grounding
N. invalid PDF span grounding

Also verifies:
- Discrepancy history is never deleted
- No duplicate active blockers are created
- Provenance is updated only on successful correction
"""

from __future__ import annotations

import json
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cli import seed_baseline
from app.models.entities import (
    DiscrepancyFlag,
    DraftLineItem,
    FieldProvenance,
    OrderDraft,
    PurchaseOrderDocument,
)
from app.services.reconciliation import (
    SourceGroundingMismatchError,
    TerminalDraftMutationError,
    correct_line_field,
    evaluate_clean_draft,
    remove_line_item,
    validate_field_value_against_snippet,
    validate_grounding,
)


@pytest.fixture
def seeded_db(db_session: Session) -> Session:
    """Provide a database session seeded with baseline catalog, contracts, and tiers."""
    seed_baseline(db_session)
    return db_session


def _create_draft_with_doc(
    db: Session,
    *,
    raw_text: str,
    content_type: str = "text/plain",
    customer_id: str = "CUST-ACME",
    line_items: list[DraftLineItem] | None = None,
    extracted_order_total_cents: int | None = None,
    status: str = "Needs Review",
) -> OrderDraft:
    """Helper to persist a test document and order draft."""
    doc = PurchaseOrderDocument(
        filename="test_doc.txt" if content_type == "text/plain" else "test_doc.pdf",
        content_type=content_type,
        raw_text=raw_text,
        status="Ingested",
    )
    db.add(doc)
    db.flush()

    if line_items is None:
        line_items = [
            DraftLineItem(
                line_number=1,
                customer_description="18in stretch film heavy duty",
                extracted_quantity=10,
                extracted_unit_price_cents=2100,
                extracted_line_total_cents=21000,
                matched_sku="SKU-WRAP-18",
                sku_confidence="High",
                sku_resolution_source="AI_HIGH_CONFIDENCE",
                status="Active",
            )
        ]

    draft = OrderDraft(
        document_id=doc.id,
        customer_id=customer_id,
        customer_name_extracted="Acme Industrial Supplies",
        po_number_extracted="PO-10023",
        extracted_order_total_cents=extracted_order_total_cents,
        status=status,
        is_replay_mode=False,
        line_items=line_items,
    )
    db.add(draft)
    db.flush()
    return draft


# =============================================================================
# A. Valid TXT Grounding
# =============================================================================

def test_a_valid_txt_grounding(seeded_db: Session) -> None:
    """A. Valid TXT grounding successfully updates field and provenance."""
    raw_text = (
        "PURCHASE ORDER\n"
        "Acme Industrial Supplies\n"
        "PO-10023\n"
        "Line 1: 10 cs 18in stretch film\n"
    )
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text)
    line = draft.line_items[0]
    line.extracted_quantity = 5  # Initial incorrect extraction

    # Line 4: "Line 1: 10 cs 18in stretch film" -> "10 cs" starts at offset 8
    target_line = raw_text.splitlines()[3]
    offset = target_line.index("10 cs")
    location = {"type": "txt", "line_number": 4, "char_offset": offset}

    correct_line_field(
        seeded_db,
        draft=draft,
        line=line,
        field="extracted_quantity",
        value=10,
        source_snippet="10 cs",
        source_location=location,
    )

    assert line.extracted_quantity == 10
    prov = next((p for p in line.provenance_records if p.field_name == "extracted_quantity"), None)
    assert prov is not None
    assert prov.verbatim_snippet == "10 cs"
    assert prov.location_type == "txt"
    assert json.loads(prov.location_data_json) == location


# =============================================================================
# B. Forged TXT Location Fails Closed
# =============================================================================

def test_b_forged_txt_location_fails(seeded_db: Session) -> None:
    """B. Forged TXT location raises SourceGroundingMismatchError fail-closed."""
    raw_text = (
        "PURCHASE ORDER\n"
        "Acme Industrial Supplies\n"
        "PO-10023\n"
        "Line 1: 10 cs stretch film\n"
    )
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text)
    line = draft.line_items[0]

    # Claim offset 0 on line 4 contains "10 cs" (actual text starts with "Line 1: ")
    forged_loc = {"type": "txt", "line_number": 4, "char_offset": 0}

    with pytest.raises(SourceGroundingMismatchError) as exc_info:
        correct_line_field(
            seeded_db,
            draft=draft,
            line=line,
            field="extracted_quantity",
            value=10,
            source_snippet="10 cs",
            source_location=forged_loc,
        )
    assert "mismatch" in str(exc_info.value).lower()

    # Out-of-bounds line number also fails closed
    out_of_bounds_loc = {"type": "txt", "line_number": 99, "char_offset": 0}
    with pytest.raises(SourceGroundingMismatchError):
        correct_line_field(
            seeded_db,
            draft=draft,
            line=line,
            field="extracted_quantity",
            value=10,
            source_snippet="10 cs",
            source_location=out_of_bounds_loc,
        )


# =============================================================================
# C. Grounded Snippet but Unsupported Requested Value Fails
# =============================================================================

def test_c_grounded_snippet_unsupported_requested_value_fails(seeded_db: Session) -> None:
    """C. Grounded snippet with unsupported value (e.g. snippet '2', requested 999) fails closed."""
    raw_text = (
        "PURCHASE ORDER\n"
        "Acme Industrial Supplies\n"
        "PO-10023\n"
        "Line 1: 2 cs stretch film\n"
    )
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text)
    line = draft.line_items[0]

    # Offset 8 contains "2 cs"
    target_line = raw_text.splitlines()[3]
    offset = target_line.index("2 cs")
    location = {"type": "txt", "line_number": 4, "char_offset": offset}

    # Snippet is genuinely "2 cs", but operator asks for 999
    with pytest.raises(SourceGroundingMismatchError) as exc_info:
        correct_line_field(
            seeded_db,
            draft=draft,
            line=line,
            field="extracted_quantity",
            value=999,
            source_snippet="2 cs",
            source_location=location,
        )
    assert "does not match" in str(exc_info.value).lower()

    # Test loose substring rejection: snippet "100", requested 10
    with pytest.raises(SourceGroundingMismatchError):
        validate_field_value_against_snippet("extracted_quantity", 10, "100")


# =============================================================================
# D. Atomic Grounding Failure Leaves Durable State Unmutated
# =============================================================================

def test_d_atomic_grounding_failure_leaves_durable_state_unmutated(seeded_db: Session) -> None:
    """D. Grounding validation failure leaves entire durable and session state unmutated."""
    raw_text = (
        "PURCHASE ORDER\n"
        "Acme Industrial Supplies\n"
        "PO-10023\n"
        "Line 1: 10 cs stretch film\n"
    )
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text)
    line = draft.line_items[0]

    initial_qty = line.extracted_quantity
    initial_price = line.extracted_unit_price_cents
    initial_total = line.extracted_line_total_cents
    initial_status = line.status
    initial_draft_status = draft.status
    initial_subtotal = draft.calculated_subtotal_cents
    initial_flags = [(f.id, f.discrepancy_type, f.resolution_state) for f in draft.discrepancy_flags]
    initial_prov_count = len(draft.provenance_records)

    dishonest_loc = {"type": "txt", "line_number": 4, "char_offset": 99}
    with pytest.raises(SourceGroundingMismatchError):
        correct_line_field(
            seeded_db,
            draft=draft,
            line=line,
            field="extracted_quantity",
            value=999,
            source_snippet="999",
            source_location=dishonest_loc,
        )

    assert line.extracted_quantity == initial_qty
    assert line.extracted_unit_price_cents == initial_price
    assert line.extracted_line_total_cents == initial_total
    assert line.status == initial_status
    assert draft.status == initial_draft_status
    assert draft.calculated_subtotal_cents == initial_subtotal
    assert [(f.id, f.discrepancy_type, f.resolution_state) for f in draft.discrepancy_flags] == initial_flags
    assert len(draft.provenance_records) == initial_prov_count


# =============================================================================
# E. Valid Money Correction
# =============================================================================

def test_e_valid_money_correction(seeded_db: Session) -> None:
    """E. Valid monetary correction converts deterministically to cents and updates provenance."""
    raw_text = (
        "PURCHASE ORDER\n"
        "Acme Industrial Supplies\n"
        "PO-10023\n"
        "Line 1: 10 cs @ $21.00 ea Total $210.00\n"
    )
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text)
    line = draft.line_items[0]
    line.extracted_unit_price_cents = 1900  # Incorrectly extracted initially

    target_line = raw_text.splitlines()[3]
    offset = target_line.index("$21.00")
    location = {"type": "txt", "line_number": 4, "char_offset": offset}

    # Pass as integer cents
    correct_line_field(
        seeded_db,
        draft=draft,
        line=line,
        field="extracted_unit_price",
        value=2100,
        source_snippet="$21.00",
        source_location=location,
    )

    assert line.extracted_unit_price_cents == 2100
    prov = next((p for p in line.provenance_records if p.field_name == "extracted_unit_price"), None)
    assert prov is not None
    assert prov.verbatim_snippet == "$21.00"

    # Also test line total correction using string "$210.00"
    total_offset = target_line.index("$210.00")
    total_loc = {"type": "txt", "line_number": 4, "char_offset": total_offset}
    correct_line_field(
        seeded_db,
        draft=draft,
        line=line,
        field="extracted_line_total",
        value="$210.00",
        source_snippet="$210.00",
        source_location=total_loc,
    )
    assert line.extracted_line_total_cents == 21000


# =============================================================================
# F. Genuine Commercial Discrepancy Remains Unresolved
# =============================================================================

def test_f_genuine_commercial_discrepancy_remains_unresolved(seeded_db: Session) -> None:
    """F. CorrectField does not override commercial contract price; PriceMismatch remains Unresolved."""
    # Contract tier price for SKU-WRAP-18 at qty 10 is $21.00 (2100 cents).
    # Customer PO states $18.00.
    raw_text = (
        "PURCHASE ORDER\n"
        "Acme Industrial Supplies\n"
        "PO-10023\n"
        "Line 1: 10 cs @ $18.00 ea Total $180.00\n"
    )
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text)
    line = draft.line_items[0]
    # Ingestion originally mis-extracted $19.00
    line.extracted_unit_price_cents = 1900
    line.extracted_line_total_cents = 18000
    evaluate_clean_draft(seeded_db, draft)

    # Initial PriceMismatch flag is present
    pm_flags = [f for f in draft.discrepancy_flags if f.discrepancy_type == "PriceMismatch"]
    assert len(pm_flags) == 1
    assert pm_flags[0].resolution_state == "Unresolved"

    # Operator correctly grounds price to actual source evidence ($18.00)
    target_line = raw_text.splitlines()[3]
    offset = target_line.index("$18.00")
    location = {"type": "txt", "line_number": 4, "char_offset": offset}

    correct_line_field(
        seeded_db,
        draft=draft,
        line=line,
        field="extracted_unit_price",
        value=1800,
        source_snippet="$18.00",
        source_location=location,
    )

    # Extraction is updated and provenance matches
    assert line.extracted_unit_price_cents == 1800

    # Contract price is 2500 cents ($25.00), so PriceMismatch MUST remain Unresolved
    pm_flags = [f for f in draft.discrepancy_flags if f.discrepancy_type == "PriceMismatch"]
    assert len(pm_flags) == 1  # No duplicate flag
    assert pm_flags[0].resolution_state == "Unresolved"
    assert pm_flags[0].requested_value == "$18.00"
    assert pm_flags[0].expected_value == "$25.00"
    assert draft.status == "Needs Review"


# =============================================================================
# G. Obsolete Discrepancy -> ResolvedByCorrection
# =============================================================================

def test_g_obsolete_discrepancy_resolved_by_correction(seeded_db: Session) -> None:
    """G. Correction that satisfies business rule transitions discrepancy to ResolvedByCorrection."""
    raw_text = (
        "PURCHASE ORDER\n"
        "Acme Industrial Supplies\n"
        "PO-10023\n"
        "Line 1: 10 cs @ $25.00 ea Total $250.00\n"
    )
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text)
    line = draft.line_items[0]
    line.extracted_quantity = 10
    line.extracted_unit_price_cents = 2500
    # Mis-extracted line total caused ArithmeticMismatch
    line.extracted_line_total_cents = 20000
    evaluate_clean_draft(seeded_db, draft)

    arith_flags = [f for f in draft.discrepancy_flags if f.discrepancy_type == "ArithmeticMismatch"]
    assert len(arith_flags) == 1
    assert arith_flags[0].resolution_state == "Unresolved"
    assert draft.status == "Needs Review"

    # Operator corrects extracted_line_total to $250.00 grounded in document
    target_line = raw_text.splitlines()[3]
    offset = target_line.index("$250.00")
    location = {"type": "txt", "line_number": 4, "char_offset": offset}

    correct_line_field(
        seeded_db,
        draft=draft,
        line=line,
        field="extracted_line_total",
        value=25000,
        source_snippet="$250.00",
        source_location=location,
    )

    # Discrepancy is marked ResolvedByCorrection, not deleted
    arith_flags = [f for f in draft.discrepancy_flags if f.discrepancy_type == "ArithmeticMismatch"]
    assert len(arith_flags) == 1
    assert arith_flags[0].resolution_state == "ResolvedByCorrection"

    # No unresolved blockers remain -> draft becomes Ready for Approval
    assert draft.status == "Ready for Approval"


# =============================================================================
# H. Line Removal -> Removed + ResolvedByLineRemoval
# =============================================================================

def test_h_line_removal_marks_status_removed_and_preserves_history(seeded_db: Session) -> None:
    """H. Removing an active line marks status=Removed and unresolved flags=ResolvedByLineRemoval."""
    raw_text = (
        "PURCHASE ORDER\n"
        "Acme Industrial Supplies\n"
        "PO-10023\n"
        "Line 1: 10 cs invalid item\n"
    )
    line = DraftLineItem(
        line_number=1,
        customer_description="invalid item",
        extracted_quantity=10,
        extracted_unit_price_cents=2100,
        extracted_line_total_cents=21000,
        matched_sku=None,
        sku_confidence="Ambiguous",
        sku_resolution_source="NONE",
        status="Active",
    )
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text, line_items=[line])
    evaluate_clean_draft(seeded_db, draft)

    cat_flags = [f for f in draft.discrepancy_flags if f.discrepancy_type == "CatalogMatchingMismatch"]
    assert len(cat_flags) == 1
    assert cat_flags[0].resolution_state == "Unresolved"

    # Remove line
    remove_line_item(seeded_db, draft=draft, line=line)

    assert line.status == "Removed"
    # History preserved: flag is ResolvedByLineRemoval, not deleted
    assert cat_flags[0].resolution_state == "ResolvedByLineRemoval"
    assert len(draft.discrepancy_flags) == 1


# =============================================================================
# I. Subtotal Recalculation Excludes Removed Line
# =============================================================================

def test_i_subtotal_recalculation_excludes_removed_line(seeded_db: Session) -> None:
    """I. Draft calculated_subtotal_cents recalculates using only active lines."""
    raw_text = (
        "PURCHASE ORDER\n"
        "Acme Industrial Supplies\n"
        "PO-10023\n"
        "Line 1: 10 cs film $250.00\n"
        "Line 2: 20 cs tape $500.00\n"
    )
    line1 = DraftLineItem(
        line_number=1,
        customer_description="18in stretch film",
        extracted_quantity=10,
        extracted_unit_price_cents=2500,
        extracted_line_total_cents=25000,
        matched_sku="SKU-WRAP-18",
        sku_confidence="High",
        sku_resolution_source="AI_HIGH_CONFIDENCE",
        status="Active",
    )
    line2 = DraftLineItem(
        line_number=2,
        customer_description="packaging tape 2in",
        extracted_quantity=20,
        extracted_unit_price_cents=2500,
        extracted_line_total_cents=50000,
        matched_sku="SKU-WRAP-18",
        sku_confidence="High",
        sku_resolution_source="AI_HIGH_CONFIDENCE",
        status="Active",
    )
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text, line_items=[line1, line2])
    evaluate_clean_draft(seeded_db, draft)

    assert line1.calculated_line_total_cents == 25000
    assert line2.calculated_line_total_cents == 50000
    assert draft.calculated_subtotal_cents == 75000

    # Remove Line 1
    remove_line_item(seeded_db, draft=draft, line=line1)

    assert draft.calculated_subtotal_cents == 50000
    assert line1.status == "Removed"
    assert line2.status == "Active"


# =============================================================================
# J. Unrelated Blocker on Remaining Active Line Stays Effective
# =============================================================================

def test_j_unrelated_blocker_on_remaining_active_line_stays_effective(seeded_db: Session) -> None:
    """J. Removing line 1 leaves line 2 blocker active; draft status remains Needs Review."""
    raw_text = (
        "PURCHASE ORDER\n"
        "Acme Industrial Supplies\n"
        "PO-10023\n"
        "Line 1: 10 cs film $210.00\n"
        "Line 2: 5 cs unknown\n"
    )
    line1 = DraftLineItem(
        line_number=1,
        customer_description="18in stretch film",
        extracted_quantity=10,
        extracted_unit_price_cents=2100,
        extracted_line_total_cents=20000,  # Arithmetic mismatch on line 1
        matched_sku="SKU-WRAP-18",
        sku_confidence="High",
        sku_resolution_source="AI_HIGH_CONFIDENCE",
        status="Active",
    )
    line2 = DraftLineItem(
        line_number=2,
        customer_description="unknown SKU item",
        extracted_quantity=5,
        extracted_unit_price_cents=1000,
        extracted_line_total_cents=5000,
        matched_sku=None,
        sku_confidence="Ambiguous",  # Blocker on line 2
        sku_resolution_source="NONE",
        status="Active",
    )
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text, line_items=[line1, line2])
    evaluate_clean_draft(seeded_db, draft)
    assert draft.status == "Needs Review"

    # Remove line 1
    remove_line_item(seeded_db, draft=draft, line=line1)

    # Line 1's arithmetic mismatch is ResolvedByLineRemoval
    l1_flag = next(f for f in draft.discrepancy_flags if f.discrepancy_type == "ArithmeticMismatch")
    assert l1_flag.resolution_state == "ResolvedByLineRemoval"

    # Line 2's CatalogMatchingMismatch is still Unresolved
    l2_flag = next(f for f in draft.discrepancy_flags if f.discrepancy_type == "CatalogMatchingMismatch")
    assert l2_flag.resolution_state == "Unresolved"

    # Draft MUST remain Needs Review
    assert draft.status == "Needs Review"


# =============================================================================
# K & L. Terminal Draft Mutation Protection (Approved / Rejected)
# =============================================================================

def test_k_approved_terminal_correction_and_removal_blocked(seeded_db: Session) -> None:
    """K. Approved draft deterministically rejects both field correction and line removal."""
    raw_text = "PURCHASE ORDER\nAcme Industrial Supplies\nPO-10023\nLine 1: 10 cs\n"
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text, status="Approved")
    line = draft.line_items[0]

    location = {"type": "txt", "line_number": 4, "char_offset": 8}
    with pytest.raises((TerminalDraftMutationError, ValueError)):
        correct_line_field(
            seeded_db,
            draft=draft,
            line=line,
            field="extracted_quantity",
            value=10,
            source_snippet="10 cs",
            source_location=location,
        )

    with pytest.raises((TerminalDraftMutationError, ValueError)):
        remove_line_item(seeded_db, draft=draft, line=line)

    assert line.status == "Active"
    assert draft.status == "Approved"


def test_l_rejected_terminal_correction_and_removal_blocked(seeded_db: Session) -> None:
    """L. Rejected draft deterministically rejects both field correction and line removal."""
    raw_text = "PURCHASE ORDER\nAcme Industrial Supplies\nPO-10023\nLine 1: 10 cs\n"
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text, status="Rejected")
    line = draft.line_items[0]

    location = {"type": "txt", "line_number": 4, "char_offset": 8}
    with pytest.raises((TerminalDraftMutationError, ValueError)):
        correct_line_field(
            seeded_db,
            draft=draft,
            line=line,
            field="extracted_quantity",
            value=10,
            source_snippet="10 cs",
            source_location=location,
        )

    with pytest.raises((TerminalDraftMutationError, ValueError)):
        remove_line_item(seeded_db, draft=draft, line=line)

    assert line.status == "Active"
    assert draft.status == "Rejected"


# =============================================================================
# M. Positive PDF Grounding
# =============================================================================

def test_m_positive_pdf_grounding(seeded_db: Session) -> None:
    """M. PDF grounding with valid char_start and char_end succeeds and stores PDF provenance."""
    raw_text = "PAGE 1\nAcme Industrial Supplies\nItem: SKU-WRAP-18 Qty: 10 cs Price: $21.00\n"
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text, content_type="application/pdf")
    line = draft.line_items[0]
    line.extracted_quantity = 5

    start = raw_text.index("10 cs")
    end = start + len("10 cs")
    location = {"type": "pdf", "page_number": 1, "char_start": start, "char_end": end}

    correct_line_field(
        seeded_db,
        draft=draft,
        line=line,
        field="extracted_quantity",
        value=10,
        source_snippet="10 cs",
        source_location=location,
    )

    assert line.extracted_quantity == 10
    prov = next((p for p in line.provenance_records if p.field_name == "extracted_quantity"), None)
    assert prov is not None
    assert prov.verbatim_snippet == "10 cs"
    assert prov.location_type == "pdf"
    assert json.loads(prov.location_data_json) == location


# =============================================================================
# N. Invalid PDF Span Grounding Fails Closed
# =============================================================================

def test_n_invalid_pdf_span_grounding_fails(seeded_db: Session) -> None:
    """N. Out-of-bounds, inverted, or text-mismatched PDF span raises SourceGroundingMismatchError."""
    raw_text = "PAGE 1\nAcme Industrial Supplies\nItem: SKU-WRAP-18 Qty: 10 cs Price: $21.00\n"
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text, content_type="application/pdf")
    line = draft.line_items[0]

    # Inverted span: char_end <= char_start
    with pytest.raises(SourceGroundingMismatchError):
        correct_line_field(
            seeded_db,
            draft=draft,
            line=line,
            field="extracted_quantity",
            value=10,
            source_snippet="10 cs",
            source_location={"type": "pdf", "page_number": 1, "char_start": 20, "char_end": 10},
        )

    # Out of range span
    with pytest.raises(SourceGroundingMismatchError):
        correct_line_field(
            seeded_db,
            draft=draft,
            line=line,
            field="extracted_quantity",
            value=10,
            source_snippet="10 cs",
            source_location={"type": "pdf", "page_number": 1, "char_start": 0, "char_end": 9999},
        )

    # Content mismatch
    with pytest.raises(SourceGroundingMismatchError):
        correct_line_field(
            seeded_db,
            draft=draft,
            line=line,
            field="extracted_quantity",
            value=10,
            source_snippet="10 cs",
            source_location={"type": "pdf", "page_number": 1, "char_start": 0, "char_end": 5},
        )


# =============================================================================
# Additional Edge Case & Scope Invariant Tests
# =============================================================================

def test_forbidden_fields_cannot_be_corrected(seeded_db: Session) -> None:
    """Grounded correction may only apply to allowed extracted fields."""
    raw_text = "PURCHASE ORDER\nAcme Industrial Supplies\nPO-10023\n"
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text)
    line = draft.line_items[0]

    location = {"type": "txt", "line_number": 1, "char_offset": 0}
    with pytest.raises(SourceGroundingMismatchError) as exc_info:
        correct_line_field(
            seeded_db,
            draft=draft,
            line=line,
            field="matched_sku",
            value="SKU-WRAP-18",
            source_snippet="PURCHASE",
            source_location=location,
        )
    assert "not an allowed" in str(exc_info.value).lower()


def test_txt_snippet_crossing_line_break_fails_closed(seeded_db: Session) -> None:
    """TXT snippets cannot cross line boundaries."""
    raw_text = "Line 1 text\nLine 2 text\n"
    location = {"type": "txt", "line_number": 1, "char_offset": 5}
    with pytest.raises(SourceGroundingMismatchError):
        validate_grounding(raw_text, "text\nLine", location)


def test_ambiguous_number_in_snippet_fails_closed() -> None:
    """Snippets with multiple candidate quantities fail closed without guessing."""
    with pytest.raises(SourceGroundingMismatchError) as exc_info:
        validate_field_value_against_snippet("extracted_quantity", 10, "10 of 20")
    assert "ambiguous" in str(exc_info.value).lower()


def test_description_correction_validates_and_updates(seeded_db: Session) -> None:
    """Correct customer_description with matching grounded snippet."""
    raw_text = (
        "PURCHASE ORDER\n"
        "Acme Industrial Supplies\n"
        "PO-10023\n"
        "18in stretch film heavy duty\n"
    )
    draft = _create_draft_with_doc(seeded_db, raw_text=raw_text)
    line = draft.line_items[0]
    line.customer_description = "old wrong description"

    location = {"type": "txt", "line_number": 4, "char_offset": 0}
    correct_line_field(
        seeded_db,
        draft=draft,
        line=line,
        field="customer_description",
        value="18in stretch film heavy duty",
        source_snippet="18in stretch film heavy duty",
        source_location=location,
    )

    assert line.customer_description == "18in stretch film heavy duty"
    prov = next((p for p in line.provenance_records if p.field_name == "customer_description"), None)
    assert prov is not None
    assert prov.verbatim_snippet == "18in stretch film heavy duty"
