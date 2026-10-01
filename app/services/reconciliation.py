"""Deterministic contract pricing tier lookup and integer-cents arithmetic engine.

Provides foundational business rule primitives for T010:
- Deterministic customer/SKU quantity-tier lookup
- Exact integer-cents line arithmetic
- Exact integer-cents subtotal arithmetic

No AI participates in pricing, arithmetic, tier selection, or fallback decisions.
"""

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    CatalogProduct, ContractPriceTier, CustomerContract, DiscrepancyFlag,
    DraftLineItem, FieldProvenance, OrderDraft, PurchaseOrderDocument,
)
from app.models.schemas import decimal_to_cents


class PricingError(Exception):
    """Base exception for deterministic pricing engine errors."""


class PricingConflictError(PricingError):
    """Raised when multiple conflicting contract tiers exist at the same threshold."""


class SourceGroundingMismatchError(Exception):
    """Raised when source-grounding validation fails during field correction."""


class TerminalDraftMutationError(ValueError):
    """Raised when attempting to mutate an Approved or Rejected draft."""


def calculate_line_total_cents(
    quantity: int,
    contract_price_cents: int,
) -> int:
    """Calculate the exact line total in integer cents.

    calculated_line_total_cents = extracted_quantity * contract_price_cents

    Uses pure integer arithmetic without floating-point intermediates.
    Rejects invalid types (e.g. bool, float) and invalid values (quantity < 1, price < 0).
    """
    if isinstance(quantity, bool) or type(quantity) is not int:
        raise TypeError(
            f"quantity must be an integer, got {type(quantity).__name__} ({quantity!r})"
        )
    if quantity < 1:
        raise ValueError(
            f"quantity must be a positive integer (>= 1), got {quantity}"
        )

    if isinstance(contract_price_cents, bool) or type(contract_price_cents) is not int:
        raise TypeError(
            f"contract_price_cents must be an integer, got {type(contract_price_cents).__name__} ({contract_price_cents!r})"
        )
    if contract_price_cents < 0:
        raise ValueError(
            f"contract_price_cents must be a nonnegative integer (>= 0), got {contract_price_cents}"
        )

    return quantity * contract_price_cents


def calculate_subtotal_cents(
    line_totals_cents: Iterable[int],
) -> int:
    """Calculate the exact order subtotal in integer cents by summing line totals.

    subtotal_cents = sum(line_total_cents)

    Uses pure integer arithmetic. Rejects non-integers, booleans, and negative values.
    Empty iterable yields 0 cents.
    """
    if isinstance(line_totals_cents, (str, bytes)):
        raise TypeError("line_totals_cents cannot be a string or bytes")
    try:
        iterator = iter(line_totals_cents)
    except TypeError as exc:
        raise TypeError(
            f"line_totals_cents must be iterable, got {type(line_totals_cents).__name__}"
        ) from exc

    subtotal = 0
    for idx, val in enumerate(iterator):
        if isinstance(val, bool) or type(val) is not int:
            raise TypeError(
                f"line_total_cents at index {idx} must be an integer, got {type(val).__name__} ({val!r})"
            )
        if val < 0:
            raise ValueError(
                f"line_total_cents at index {idx} must be a nonnegative integer (>= 0), got {val}"
            )
        subtotal += val

    return subtotal


def select_contract_price_tier(
    db: Session,
    customer_id: str,
    sku: str,
    quantity: int,
) -> ContractPriceTier | None:
    """Select the eligible contract price tier for a customer and SKU based on quantity.

    Frozen Tier Selection Rule:
    Given:
      - customer C
      - SKU S
      - requested quantity Q

    1. Query ContractPriceTier records satisfying:
         contract.customer_id == C
         sku == S
         min_quantity <= Q
    2. Fail closed if eligible tiers belong to more than one contract (PricingConflictError).
    3. Select the eligible tier having the maximum min_quantity within the single contract.
    4. If no tier has min_quantity <= Q, returns None (explicit no-tier result).

    Does not apply automatic fallback to base price or lower tiers.
    """
    if not isinstance(db, Session):
        raise TypeError(f"db must be a sqlalchemy.orm.Session, got {type(db).__name__}")

    if isinstance(customer_id, bool) or not isinstance(customer_id, str):
        raise TypeError(
            f"customer_id must be a string, got {type(customer_id).__name__}"
        )
    if len(customer_id) == 0:
        raise ValueError("customer_id cannot be empty")

    if isinstance(sku, bool) or not isinstance(sku, str):
        raise TypeError(f"sku must be a string, got {type(sku).__name__}")
    if len(sku) == 0:
        raise ValueError("sku cannot be empty")

    if isinstance(quantity, bool) or type(quantity) is not int:
        raise TypeError(
            f"quantity must be an integer, got {type(quantity).__name__} ({quantity!r})"
        )
    if quantity < 1:
        raise ValueError(
            f"quantity must be a positive integer (>= 1), got {quantity}"
        )

    stmt = (
        select(ContractPriceTier)
        .join(CustomerContract, ContractPriceTier.contract_id == CustomerContract.id)
        .where(
            CustomerContract.customer_id == customer_id,
            ContractPriceTier.sku == sku,
            ContractPriceTier.min_quantity <= quantity,
        )
        .order_by(ContractPriceTier.min_quantity.desc())
    )

    eligible_tiers = db.scalars(stmt).all()
    if not eligible_tiers:
        return None

    contract_ids = {tier.contract_id for tier in eligible_tiers}
    if len(contract_ids) > 1:
        formatted_ids = ", ".join(sorted(contract_ids))
        raise PricingConflictError(
            f"Ambiguous contract pricing: multiple contracts ({formatted_ids}) "
            f"found for customer '{customer_id}' and SKU '{sku}'"
        )

    return eligible_tiers[0]


def _format_money(cents: int) -> str:
    """Format integer cents deterministically without floating-point conversion."""
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{sign}${dollars}.{remainder:02d}"


def _matches_line(flag: DiscrepancyFlag, line: DraftLineItem | None) -> bool:
    """Check whether a discrepancy flag matches a line item identity (in-memory or persisted)."""
    if line is None:
        return flag.line_item is None and flag.line_item_id is None
    return flag.line_item is line or (line.id is not None and flag.line_item_id == line.id)


def _ensure_unresolved_discrepancy(
    draft: OrderDraft,
    line: DraftLineItem | None,
    discrepancy_type: str,
    expected_value: str,
    requested_value: str,
    explanation: str,
) -> None:
    """Append or update an unresolved blocker by ORM line identity, preserving existing history.

    Relationship identity also distinguishes newly created lines before their
    primary keys are assigned. The caller keeps this operation under no_autoflush.
    """
    for flag in draft.discrepancy_flags:
        if (
            flag.resolution_state == "Unresolved"
            and flag.discrepancy_type == discrepancy_type
            and _matches_line(flag, line)
        ):
            flag.expected_value = expected_value
            flag.requested_value = requested_value
            flag.explanation = explanation
            return
    draft.discrepancy_flags.append(DiscrepancyFlag(
        line_item=line, discrepancy_type=discrepancy_type,
        severity="Blocking", resolution_state="Unresolved",
        expected_value=expected_value, requested_value=requested_value,
        explanation=explanation,
    ))


def evaluate_clean_draft(db: Session, draft: OrderDraft) -> OrderDraft:
    """Evaluate P2 discrepancies and readiness with deterministic integer rules.

    Recompute contract pricing, append missing unresolved blockers and set draft
    readiness without modifying discrepancy history. Obsolete unresolved blockers
    are transitioned to ResolvedByCorrection (or ResolvedByLineRemoval for removed lines).
    Customer-source arithmetic stays independent from contract pricing. The caller owns
    flush, commit and rollback; evaluation never calls AI or external services. Incomplete
    input remains Needs Review even when no discrepancy can be established.
    """
    if draft.status in ("Approved", "Rejected"):
        raise TerminalDraftMutationError("Cannot evaluate a terminal draft")

    def present(value: str | None) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def valid_money(value: int | None) -> bool:
        return type(value) is int and value >= 0

    # Evaluation must not flush an unverified SKU hint into a foreign key.
    with db.no_autoflush:
        established_discrepancies: list[tuple[DraftLineItem | None, str]] = []

        def record_discrepancy(
            line: DraftLineItem | None,
            discrepancy_type: str,
            expected_value: str,
            requested_value: str,
            explanation: str,
        ) -> None:
            established_discrepancies.append((line, discrepancy_type))
            _ensure_unresolved_discrepancy(
                draft, line, discrepancy_type,
                expected_value, requested_value, explanation,
            )

        known_customer = present(draft.customer_id) and db.scalar(
            select(CustomerContract.id).where(CustomerContract.customer_id == draft.customer_id).limit(1)
        ) is not None
        active_lines = [line for line in draft.line_items if line.status == "Active"]
        clean = bool(
            known_customer and present(draft.customer_name_extracted)
            and present(draft.po_number_extracted) and active_lines
        )
        for line in active_lines:
            # Clear stale derived values before any failed lookup can reuse them.
            line.contract_price_cents = None
            line.calculated_line_total_cents = 0
            quantity = line.extracted_quantity
            valid_quantity = type(quantity) is int and quantity >= 1
            product = db.get(CatalogProduct, line.matched_sku) if present(line.matched_sku) else None
            catalog_resolved = product is not None and (
                line.sku_resolution_source == "OPERATOR_SELECTED"
                or (
                    line.sku_resolution_source == "AI_HIGH_CONFIDENCE"
                    and line.sku_confidence == "High"
                )
            )
            if not catalog_resolved:
                clean = False
                # Missing internal resolution metadata alone is incomplete,
                # rather than evidence of an ambiguous/unrecognized mapping.
                if product is None or line.sku_confidence in ("Ambiguous", "Unrecognized"):
                    record_discrepancy(
                        line, "CatalogMatchingMismatch",
                        "Valid catalog SKU resolved by high-confidence AI or operator selection",
                        f"SKU: {line.matched_sku or 'missing'}; confidence: {line.sku_confidence or 'missing'}; "
                        f"resolution: {line.sku_resolution_source or 'missing'}",
                        "The SKU is missing, absent from the catalog, or ambiguous/unrecognized "
                        "without a valid operator selection. Explicit catalog resolution is required.",
                    )

            if product is not None and type(quantity) is int:
                violations = []
                if quantity < product.min_order_quantity:
                    violations.append(f"Quantity {quantity} is below MOQ {product.min_order_quantity}")
                if quantity % product.package_increment != 0:
                    violations.append(
                        f"Quantity {quantity} is not a multiple of package increment {product.package_increment}"
                    )
                if violations:
                    record_discrepancy(
                        line, "QuantityOrPackagingBreach",
                        f"MOQ: {product.min_order_quantity}; package increment: {product.package_increment}",
                        f"Qty: {quantity}", "; ".join(violations) + ".",
                    )

            source_price_valid = valid_money(line.extracted_unit_price_cents)
            source_total_valid = valid_money(line.extracted_line_total_cents)
            if not (
                present(line.customer_description) and valid_quantity
                and source_price_valid and source_total_valid
            ):
                clean = False
            if valid_quantity and source_price_valid and source_total_valid:
                source_line_total = calculate_line_total_cents(quantity, line.extracted_unit_price_cents)
                if source_line_total != line.extracted_line_total_cents:
                    record_discrepancy(
                        line, "ArithmeticMismatch",
                        _format_money(source_line_total), _format_money(line.extracted_line_total_cents),
                        f"Customer-stated line total differs from quantity {quantity} multiplied by "
                        f"customer-stated unit price {_format_money(line.extracted_unit_price_cents)}.",
                    )

            resolvable = (
                known_customer and valid_quantity and catalog_resolved
            )
            if not resolvable:
                clean = False
                continue
            try:
                tier = select_contract_price_tier(
                    db, draft.customer_id, line.matched_sku, line.extracted_quantity,
                )
            except PricingConflictError:
                # No arbitrary contract choice or commercial fallback is made.
                clean = False
                continue
            if tier is None:
                clean = False
                continue
            line.contract_price_cents = tier.tier_price_cents
            line.calculated_line_total_cents = calculate_line_total_cents(
                line.extracted_quantity, line.contract_price_cents,
            )
            if source_price_valid and line.extracted_unit_price_cents != line.contract_price_cents:
                record_discrepancy(
                    line, "PriceMismatch",
                    _format_money(line.contract_price_cents), _format_money(line.extracted_unit_price_cents),
                    "Customer-stated unit price differs from the authoritative customer contract tier "
                    f"price for SKU {line.matched_sku} at quantity {quantity}.",
                )
        draft.calculated_subtotal_cents = calculate_subtotal_cents(
            line.calculated_line_total_cents for line in active_lines
        )
        if draft.extracted_order_total_cents is not None:
            if not valid_money(draft.extracted_order_total_cents) or not all(
                valid_money(line.extracted_line_total_cents) for line in active_lines
            ):
                clean = False
            elif active_lines:
                source_order_total = calculate_subtotal_cents(
                    line.extracted_line_total_cents for line in active_lines
                )
                if source_order_total != draft.extracted_order_total_cents:
                    record_discrepancy(
                        None, "ArithmeticMismatch",
                        _format_money(source_order_total), _format_money(draft.extracted_order_total_cents),
                        "Customer-stated order total differs from the sum of customer-stated "
                        "line totals for active lines.",
                    )
        if any(flag.resolution_state == "Unresolved" for flag in draft.discrepancy_flags):
            clean = False
        draft.status = "Ready for Approval" if clean else "Needs Review"
    return draft


def validate_grounding(
    raw_text: str,
    snippet: str,
    location: dict | Any,
) -> None:
    """Validate that verbatim source snippet exists at claimed location in canonical raw_text.

    Fails closed (SourceGroundingMismatchError) on any mismatch, out-of-range bounds,
    or malformed location data. Never searches or guesses another location.
    """
    if not isinstance(raw_text, str) or not raw_text:
        raise SourceGroundingMismatchError("Canonical document raw_text is missing or empty")
    if not isinstance(snippet, str) or not snippet:
        raise SourceGroundingMismatchError("verbatim_snippet must be a non-empty string")

    if hasattr(location, "model_dump"):
        loc = location.model_dump()
    elif isinstance(location, dict):
        loc = location
    else:
        raise SourceGroundingMismatchError(f"Invalid location type: {type(location).__name__}")

    loc_type = loc.get("type")
    if loc_type == "txt":
        line_number = loc.get("line_number")
        char_offset = loc.get("char_offset")
        if isinstance(line_number, bool) or type(line_number) is not int or line_number < 1:
            raise SourceGroundingMismatchError(f"Invalid line_number: {line_number!r}")
        if isinstance(char_offset, bool) or type(char_offset) is not int or char_offset < 0:
            raise SourceGroundingMismatchError(f"Invalid char_offset: {char_offset!r}")

        if "\n" in snippet or "\r" in snippet:
            raise SourceGroundingMismatchError("Snippet cannot contain newline characters for TXT location")

        lines = [line.rstrip("\r\n") for line in raw_text.splitlines(keepends=True)]
        if line_number > len(lines):
            raise SourceGroundingMismatchError(
                f"Line number {line_number} is out of bounds (document has {len(lines)} lines)"
            )

        target_line = lines[line_number - 1]
        start = char_offset
        end = start + len(snippet)
        if end > len(target_line):
            raise SourceGroundingMismatchError(
                f"Snippet exceeds line length ({len(target_line)}) at line {line_number}, offset {char_offset}"
            )
        if target_line[start:end] != snippet:
            raise SourceGroundingMismatchError(
                f"Snippet mismatch at line {line_number}, offset {char_offset}: "
                f"expected {snippet!r}, found {target_line[start:end]!r}"
            )

    elif loc_type == "pdf":
        page_number = loc.get("page_number")
        char_start = loc.get("char_start")
        char_end = loc.get("char_end")
        if isinstance(page_number, bool) or type(page_number) is not int or page_number < 1:
            raise SourceGroundingMismatchError(f"Invalid page_number: {page_number!r}")
        if isinstance(char_start, bool) or type(char_start) is not int or char_start < 0:
            raise SourceGroundingMismatchError(f"Invalid char_start: {char_start!r}")
        if isinstance(char_end, bool) or type(char_end) is not int or char_end <= char_start:
            raise SourceGroundingMismatchError(f"Invalid char_end: {char_end!r} (must be > char_start {char_start})")
        if char_end > len(raw_text):
            raise SourceGroundingMismatchError(
                f"Span end {char_end} exceeds document raw_text length {len(raw_text)}"
            )
        if len(snippet) != (char_end - char_start):
            raise SourceGroundingMismatchError(
                f"Snippet length {len(snippet)} does not match span length {char_end - char_start}"
            )
        if raw_text[char_start:char_end] != snippet:
            raise SourceGroundingMismatchError(
                f"Snippet mismatch at PDF span [{char_start}:{char_end}]: "
                f"expected {snippet!r}, found {raw_text[char_start:char_end]!r}"
            )
    else:
        raise SourceGroundingMismatchError(f"Unsupported location type: {loc_type!r}")


def validate_field_value_against_snippet(
    field: str,
    value: Any,
    snippet: str,
) -> tuple[str, Any]:
    """Validate that requested value is deterministically supported by source_snippet.

    Returns (canonical_field_name, normalized_value).
    Fails closed (SourceGroundingMismatchError) if value is unsupported,
    ambiguous, or not an allowed source-extracted field.
    """
    if field == "customer_description":
        if not isinstance(value, str) or not value.strip():
            raise SourceGroundingMismatchError("customer_description must be a non-empty string")
        if value.strip() != snippet.strip():
            raise SourceGroundingMismatchError(
                f"Requested description {value!r} does not match grounded snippet {snippet!r}"
            )
        return "customer_description", value

    elif field == "extracted_quantity":
        if isinstance(value, bool):
            raise SourceGroundingMismatchError("Quantity cannot be boolean")
        if isinstance(value, str):
            try:
                req_qty = int(value.strip())
            except ValueError:
                raise SourceGroundingMismatchError(f"Invalid integer quantity string: {value!r}")
        elif type(value) is int:
            req_qty = value
        elif isinstance(value, Decimal):
            if value != int(value):
                raise SourceGroundingMismatchError(f"Quantity must be a whole integer: {value!r}")
            req_qty = int(value)
        else:
            raise SourceGroundingMismatchError(f"Unsupported quantity type: {type(value).__name__}")

        if req_qty < 1:
            raise SourceGroundingMismatchError(f"Quantity must be a positive integer (>= 1), got {req_qty}")

        candidates = [int(x) for x in re.findall(r"(?<![\d.])(\d+)(?![\d.])", snippet)]
        distinct = set(candidates)
        if len(distinct) == 0:
            raise SourceGroundingMismatchError(f"No integer quantity found in snippet: {snippet!r}")
        if len(distinct) > 1:
            raise SourceGroundingMismatchError(
                f"Ambiguous quantity in snippet: {snippet!r} (found multiple distinct candidates: {sorted(distinct)})"
            )
        snippet_qty = candidates[0]
        if req_qty != snippet_qty:
            raise SourceGroundingMismatchError(
                f"Requested quantity {req_qty} does not match grounded snippet quantity {snippet_qty}"
            )
        return "extracted_quantity", req_qty

    elif field in ("extracted_unit_price", "extracted_unit_price_cents", "extracted_line_total", "extracted_line_total_cents"):
        canonical_name = "extracted_unit_price" if "unit_price" in field else "extracted_line_total"

        dollar_matches = list(re.finditer(r"\$\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", snippet))
        cents_candidates = []
        if dollar_matches:
            for m in dollar_matches:
                s = m.group(1).replace(",", "")
                d = Decimal(s)
                cents_candidates.append(decimal_to_cents(d) if "." in s else int(d * 100))
        else:
            dec_matches = list(re.finditer(r"(?<![\d.])(\d+(?:,\d{3})*\.\d{2})(?![\d.])", snippet))
            for m in dec_matches:
                s = m.group(1).replace(",", "")
                cents_candidates.append(decimal_to_cents(Decimal(s)))

        distinct_cents = set(cents_candidates)
        if len(distinct_cents) == 0:
            raise SourceGroundingMismatchError(f"No monetary amount found in snippet: {snippet!r}")
        if len(distinct_cents) > 1:
            raise SourceGroundingMismatchError(
                f"Ambiguous monetary amount in snippet: {snippet!r} (found multiple distinct values: {sorted(distinct_cents)})"
            )
        snippet_cents = cents_candidates[0]

        if isinstance(value, bool):
            raise SourceGroundingMismatchError("Money cannot be boolean")
        if isinstance(value, str):
            clean = value.strip().lstrip("$").replace(",", "").strip()
            try:
                dec = Decimal(clean)
            except InvalidOperation:
                raise SourceGroundingMismatchError(f"Invalid monetary string: {value!r}")
            if "." in clean:
                req_cents = decimal_to_cents(dec)
            else:
                if int(dec * 100) == snippet_cents:
                    req_cents = snippet_cents
                else:
                    req_cents = int(dec)
        elif type(value) is int:
            if value == snippet_cents:
                req_cents = value
            elif value * 100 == snippet_cents:
                req_cents = snippet_cents
            else:
                req_cents = value
        elif isinstance(value, Decimal):
            req_cents = decimal_to_cents(value)
        else:
            raise SourceGroundingMismatchError(f"Unsupported money type: {type(value).__name__}")

        if req_cents < 0:
            raise SourceGroundingMismatchError(f"Money cannot be negative, got {req_cents}")

        if req_cents != snippet_cents:
            raise SourceGroundingMismatchError(
                f"Requested monetary value ({req_cents} cents) does not match grounded snippet ({snippet_cents} cents)"
            )
        return canonical_name, req_cents

    else:
        raise SourceGroundingMismatchError(
            f"Field '{field}' is not an allowed source-extracted field for grounded correction"
        )


def _resolve_obsolete_discrepancies_after_correction(
    db: Session,
    draft: OrderDraft,
    line: DraftLineItem,
) -> None:
    """Transition unresolved discrepancies on the corrected line or draft to ResolvedByCorrection

    if the corrected values no longer violate the business rule constraints.
    """
    valid_quantity = type(line.extracted_quantity) is int and line.extracted_quantity >= 1
    source_price_valid = type(line.extracted_unit_price_cents) is int and line.extracted_unit_price_cents >= 0
    source_total_valid = type(line.extracted_line_total_cents) is int and line.extracted_line_total_cents >= 0

    # 1. Line-level ArithmeticMismatch
    if valid_quantity and source_price_valid and source_total_valid:
        expected_line_total = calculate_line_total_cents(line.extracted_quantity, line.extracted_unit_price_cents)
        if line.extracted_line_total_cents == expected_line_total:
            for flag in draft.discrepancy_flags:
                if (
                    flag.resolution_state == "Unresolved"
                    and flag.discrepancy_type == "ArithmeticMismatch"
                    and _matches_line(flag, line)
                ):
                    flag.resolution_state = "ResolvedByCorrection"

    # 2. QuantityOrPackagingBreach
    if line.matched_sku and valid_quantity:
        product = db.get(CatalogProduct, line.matched_sku)
        if product is not None:
            moq_met = line.extracted_quantity >= product.min_order_quantity
            pkg_met = (line.extracted_quantity % product.package_increment) == 0
            if moq_met and pkg_met:
                for flag in draft.discrepancy_flags:
                    if (
                        flag.resolution_state == "Unresolved"
                        and flag.discrepancy_type == "QuantityOrPackagingBreach"
                        and _matches_line(flag, line)
                    ):
                        flag.resolution_state = "ResolvedByCorrection"

    # 3. PriceMismatch
    if draft.customer_id and line.matched_sku and valid_quantity and source_price_valid:
        try:
            tier = select_contract_price_tier(db, draft.customer_id, line.matched_sku, line.extracted_quantity)
        except PricingConflictError:
            tier = None
        if tier is not None and line.extracted_unit_price_cents == tier.tier_price_cents:
            for flag in draft.discrepancy_flags:
                if (
                    flag.resolution_state == "Unresolved"
                    and flag.discrepancy_type == "PriceMismatch"
                    and _matches_line(flag, line)
                ):
                    flag.resolution_state = "ResolvedByCorrection"

    # 4. Order-level ArithmeticMismatch
    active_lines = [l for l in draft.line_items if l.status == "Active"]
    if draft.extracted_order_total_cents is not None and active_lines:
        if all(type(l.extracted_line_total_cents) is int and l.extracted_line_total_cents >= 0 for l in active_lines):
            order_sum = calculate_subtotal_cents(l.extracted_line_total_cents for l in active_lines)
            if order_sum == draft.extracted_order_total_cents:
                for flag in draft.discrepancy_flags:
                    if (
                        flag.resolution_state == "Unresolved"
                        and flag.discrepancy_type == "ArithmeticMismatch"
                        and flag.line_item is None
                        and flag.line_item_id is None
                    ):
                        flag.resolution_state = "ResolvedByCorrection"


def correct_line_field(
    db: Session,
    *,
    draft_id: str | None = None,
    line_id: str | None = None,
    draft: OrderDraft | None = None,
    line: DraftLineItem | None = None,
    field: str,
    value: Any,
    source_snippet: str | None = None,
    snippet: str | None = None,
    source_location: dict | Any = None,
    location: dict | Any = None,
) -> OrderDraft:
    """Deterministically correct a source-extracted line item field.

    Validates:
    1. Draft is not in terminal state (Approved or Rejected).
    2. Line item is active.
    3. Grounding against canonical PurchaseOrderDocument.raw_text.
    4. Requested value against grounded source_snippet.

    On validation failure, fails closed with SourceGroundingMismatchError
    leaving all durable and session state unmutated (atomicity).

    On success:
    1. Mutates the line item field.
    2. Updates FieldProvenance.
    3. Re-evaluates draft reconciliation (evaluate_clean_draft).
    Does NOT commit the transaction (caller owns transaction boundary).
    """
    if not isinstance(db, Session):
        raise TypeError(f"db must be a sqlalchemy.orm.Session, got {type(db).__name__}")

    effective_snippet = source_snippet if source_snippet is not None else snippet
    effective_location = source_location if source_location is not None else location

    if effective_snippet is None:
        raise SourceGroundingMismatchError("source_snippet is required")
    if effective_location is None:
        raise SourceGroundingMismatchError("source_location is required")

    # Locate draft
    if draft is None:
        if draft_id is None:
            raise ValueError("Either draft or draft_id must be provided")
        draft = db.get(OrderDraft, draft_id)
        if draft is None:
            raise ValueError(f"OrderDraft '{draft_id}' not found")

    # Check terminal state protection BEFORE ANY MUTATION
    if draft.status in ("Approved", "Rejected"):
        raise TerminalDraftMutationError(
            f"Cannot mutate draft '{draft.id}' in terminal state '{draft.status}'"
        )

    # Locate line
    if line is None:
        if line_id is None:
            raise ValueError("Either line or line_id must be provided")
        line = next((l for l in draft.line_items if l.id == line_id), None)
        if line is None:
            line = db.get(DraftLineItem, line_id)
        if line is None:
            raise ValueError(f"DraftLineItem '{line_id}' not found")

    if line.draft_id is not None and draft.id is not None and line.draft_id != draft.id:
        raise ValueError(f"Line '{line.id}' does not belong to draft '{draft.id}'")

    if line.status != "Active":
        raise ValueError(f"Cannot correct line item '{line.id}' with status '{line.status}'")

    # Locate raw_text
    doc = draft.document
    if doc is None and draft.document_id is not None:
        doc = db.get(PurchaseOrderDocument, draft.document_id)
    if doc is None or not doc.raw_text:
        raise SourceGroundingMismatchError("Canonical document raw_text is missing or empty")

    # 1. Grounding validation (fails closed)
    validate_grounding(doc.raw_text, effective_snippet, effective_location)

    # 2. Value vs snippet validation (fails closed)
    canonical_field, normalized_val = validate_field_value_against_snippet(
        field, value, effective_snippet,
    )

    # All validations passed! Apply mutation.
    if canonical_field == "customer_description":
        line.customer_description = normalized_val
    elif canonical_field == "extracted_quantity":
        line.extracted_quantity = normalized_val
    elif canonical_field == "extracted_unit_price":
        line.extracted_unit_price_cents = normalized_val
    elif canonical_field == "extracted_line_total":
        line.extracted_line_total_cents = normalized_val

    # Update or add FieldProvenance
    loc_dict = effective_location.model_dump() if hasattr(effective_location, "model_dump") else effective_location
    loc_type = loc_dict.get("type", "txt")
    loc_json = json.dumps(loc_dict, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    existing_prov = next(
        (p for p in line.provenance_records if p.field_name == canonical_field), None
    )
    if existing_prov is None:
        existing_prov = next(
            (p for p in draft.provenance_records if p.line_item_id == line.id and p.field_name == canonical_field), None
        )

    if existing_prov is not None:
        existing_prov.verbatim_snippet = effective_snippet
        existing_prov.location_type = loc_type
        existing_prov.location_data_json = loc_json
    else:
        new_prov = FieldProvenance(
            draft_id=draft.id,
            line_item_id=line.id,
            draft=draft,
            line_item=line,
            field_name=canonical_field,
            verbatim_snippet=effective_snippet,
            location_type=loc_type,
            location_data_json=loc_json,
        )
        db.add(new_prov)
        line.provenance_records.append(new_prov)
        if new_prov not in draft.provenance_records:
            draft.provenance_records.append(new_prov)

    # Resolve any discrepancies that are now valid
    _resolve_obsolete_discrepancies_after_correction(db, draft, line)

    # Re-evaluate reconciliation
    evaluate_clean_draft(db, draft)
    return draft


def remove_line_item(
    db: Session,
    *,
    draft_id: str | None = None,
    line_id: str | None = None,
    draft: OrderDraft | None = None,
    line: DraftLineItem | None = None,
) -> OrderDraft:
    """Deterministically remove an active draft line item.

    Sets DraftLineItem.status = "Removed", marks any currently unresolved
    discrepancies on that line as ResolvedByLineRemoval, and recalculates
    draft reconciliation, subtotal, and readiness excluding the removed line.

    Preserves line, provenance, and discrepancy history (no rows deleted).
    Fails with TerminalDraftMutationError if draft is Approved or Rejected.
    Caller owns transaction commit.
    """
    if not isinstance(db, Session):
        raise TypeError(f"db must be a sqlalchemy.orm.Session, got {type(db).__name__}")

    # Locate draft
    if draft is None:
        if draft_id is None:
            raise ValueError("Either draft or draft_id must be provided")
        draft = db.get(OrderDraft, draft_id)
        if draft is None:
            raise ValueError(f"OrderDraft '{draft_id}' not found")

    # Terminal draft protection
    if draft.status in ("Approved", "Rejected"):
        raise TerminalDraftMutationError(
            f"Cannot mutate draft '{draft.id}' in terminal state '{draft.status}'"
        )

    # Locate line
    if line is None:
        if line_id is None:
            raise ValueError("Either line or line_id must be provided")
        line = next((l for l in draft.line_items if l.id == line_id), None)
        if line is None:
            line = db.get(DraftLineItem, line_id)
        if line is None:
            raise ValueError(f"DraftLineItem '{line_id}' not found")

    if line.draft_id is not None and draft.id is not None and line.draft_id != draft.id:
        raise ValueError(f"Line '{line.id}' does not belong to draft '{draft.id}'")

    if line.status != "Active":
        raise ValueError(f"Cannot remove line item '{line.id}' with status '{line.status}'")

    # Set status = "Removed"
    line.status = "Removed"

    # Transition unresolved flags on this line to ResolvedByLineRemoval
    for flag in draft.discrepancy_flags:
        if flag.resolution_state == "Unresolved":
            if flag.line_item is line or (line.id is not None and flag.line_item_id == line.id):
                flag.resolution_state = "ResolvedByLineRemoval"

    # Re-evaluate reconciliation
    evaluate_clean_draft(db, draft)
    return draft
