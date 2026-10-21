# OrderShield: engineering an AI-assisted reconciliation boundary

[Project and replay quickstart](../README.md) · [Evaluation protocol](evaluation/protocol.md) · [Replay capture](images/README.md)

## Problem and product goal

A wholesale purchase order mixes document interpretation with commercial decisions.
Customer descriptions may be ambiguous, while quantities, packaging increments, and
negotiated prices need exact treatment. An LLM can suggest what the customer wrote;
it should not invent a contract price or decide that an order is approved. OrderShield
separates these responsibilities and makes unresolved discrepancies visible to an operator.

This is a team-built local MVP, not a deployed purchasing service. It accepts text and
text-extractable PDFs, extracts fields and candidate SKUs, reconciles them against seeded
catalog/contracts, and supports explicit approval or rejection. The committed vanilla
JavaScript workspace talks to FastAPI; SQLAlchemy persists drafts, provenance, audit events,
and approved-order snapshots in SQLite. The existing
[architecture and trust model](../README.md#architecture--trust-model) describes this flow.

## Implemented MVP and demo boundary

There are two distinct intake paths. [Live intake](../app/api/routes_orders.py) invokes
the selected provider. [Fixture intake](../app/api/routes_fixtures.py) reads only registered,
committed extraction records and their matching synthetic documents. Replay still runs
the actual reconciliation and lifecycle services; it is not fresh inference. Persisted
provider identity drives the non-dismissable replay banner shown in the
[authentic local screenshot](images/ordershield-replay.webp).

The demo can exercise a clean order, ambiguity resolution, or a discrepancy sequence.
For example, selecting a SKU for an ambiguous line can reveal a previously latent MOQ
breach. Resolving a semantic ambiguity does not waive a commercial rule. The
[quickstart scenarios](../tests/integration/test_quickstart_scenarios.py) encode these
workflows, including blocked approval and explicit rejection.

## AI integration and trust boundary

The [provider boundary](../app/services/ai_provider.py) implements Qwen and Gemini
adapters behind `OrderShieldAIProvider`. The runtime uses the explicitly configured
provider/model and one request, without automatic retry, provider failover, or substitution
of replay data. Qwen additionally requires its approved workspace-specific Singapore
endpoint; an API key alone is insufficient. These are implemented integrations, not a
claim that either external service is currently available.

Schema validation rejects malformed extraction. The intake service checks field citations
against canonical document text and locations before persistence. Provider/configuration
or transport failures surface as HTTP 503; invalid model output surfaces as 502, and
unextractable documents as 400. The [API assembly](../app/main.py) owns those mappings.
Source text is treated as untrusted input, and the semantic catalog sent to the provider
excludes commercial pricing rules. Grounding proves a citation matches text; it does not
prove that a semantically similar SKU is the customer's intended item.

## Deterministic business rules

[Reconciliation](../app/services/reconciliation.py) performs contract-tier lookup, integer-cent
money arithmetic, MOQ/package-increment checks, and source-stated line/order arithmetic.
The authoritative customer contract tier is separate from the price stated on the PO.
Missing or conflicting pricing cannot quietly fall back to a catalog base price.

Four blocking discrepancy types cover price, quantity/packaging, arithmetic, and catalog
matching. Operator corrections require source-grounded values; SKU selection and line
removal trigger re-evaluation. Idempotent evaluation preserves discrepancy history rather
than duplicating flags on repeated checks. [Rule tests](../tests/unit/test_reconciliation.py)
and [mutation-route tests](../tests/integration/test_p2_mutation_routes.py) make the boundary
inspectable. No model performs the commercial arithmetic or approval decision.

## State management and provenance

The [order service](../app/services/order_service.py) installs a draft only after parsing,
extraction validation, and grounding. Approval requires `Ready for Approval` and no
unresolved blocking flags; an explicit operator action creates a verified-order snapshot.
Approved and rejected drafts reject further lifecycle mutations through the application.
Audit events retain provider/model identity and operator actions; source evidence remains
linked to fields and lines. This is application-level governance, not cryptographic
tamper-proof storage or enterprise authorization: operator identity is currently a string.

## Testing and evaluation methodology

The [evaluation runner](../app/evaluation/runner.py) separates deterministic rules, replay,
historical bake-off evidence, and opt-in live inference. `ALL` runs the three offline modes;
it includes LIVE only when explicitly enabled. The versioned
[manifest](evaluation/ground_truth_manifest.json) distinguishes historical spike and current
application schemas. Running the evaluator writes result artifacts; it is not needed to
open the replay demo.

The committed [2 October aggregate](evaluation/results/20261002-001259-ALL.json) is historical
evidence, not a fresh benchmark. Its Qwen historical subset reports 6/6 schema-valid attempts,
47/48 exact mandatory fields, 3/3 determinate SKU matches, and 3/3 review-trap routes.
One Qwen historical case still fails with `SEMANTIC_WRONG`; secondary Gemini failures are
also retained. These tiny, selected synthetic denominators cannot establish general model
accuracy. Current-app seeded discrepancy detection is 5/5 in that recorded run, not a
real-world error rate.

SC-001 and SC-006 remain `PARTIALLY_MEASURED` in the aggregate. The two-line replay timing
is diagnostic, not the canonical five-line assisted-human measurement. A
[manual-study packet](evaluation/manual_study/README.md) exists, but no completed participant
results were found in the inspected public tree. No measured time-saving percentage or
production latency claim follows. [CI](../.github/workflows/ci.yml) compiles sources and runs
pytest; test conformance and empirical AI evaluation remain separate evidence.

## Team organization and verified contributions

Vladimir Barbalat's **Team Lead** role is owner-supplied. Repository history supports his
backend/provider integration [#16](https://github.com/Slave-of-Skynet/training_best2026/pull/16),
discrepancy engine [#22](https://github.com/Slave-of-Skynet/training_best2026/pull/22), mutation
adapters [#29](https://github.com/Slave-of-Skynet/training_best2026/pull/29), and reproducible
runner [#48](https://github.com/Slave-of-Skynet/training_best2026/pull/48).
The final integration [#37](https://github.com/Slave-of-Skynet/training_best2026/pull/37)
contains multiple authors; it is not evidence of sole implementation.

The [team credits](../README.md#team-contributions) link @arseniistrokov's UI, grounding, and mutation
work; @vicuvi1's bootstrap/catalog/audit APIs; @tikotto's configuration; and Mr-Ressentiment's
test harness and fixtures. Leadership, integration, and subsystem authorship are distinct.

## Limitations and future work

The MVP lacks production authentication/RBAC, schema migrations, distributed coordination,
and production deployment evidence. Textless/scanned PDFs have no OCR path. Deterministic
checks cannot eliminate wrong-but-plausible semantic matches. Broader evaluation, an
uncontaminated human baseline, and operational hardening remain future work, not delivered
capabilities or a deployment approval.
