# Replay screenshot provenance

[Project](../../README.md) · [Case study](../portfolio.md)

[ordershield-replay.webp](ordershield-replay.webp) is a **1440 × 1100** viewport capture
(48,470 bytes), taken on **22 October 2026 at about 02:41 Europe/Chisinau**
(`2026-10-21T23:41:15.326Z`) using headless Playwright Chromium, device scale 1,
and a fresh browser context.

The actual application ran locally at `http://127.0.0.1:8765/` from baseline
`813dc103dcc73b0a8b06e3a8087368b3e250707b`, with `requirements.lock` installed in
an isolated Python environment and a disposable, seeded SQLite database outside
the checkout. Provider credentials were unset/empty and no live inference was called.
All observed browser requests stayed on that localhost origin.

Normal UI controls ingested `fixture-discrepancy-apex`, using the committed synthetic
`po_discrepancy_apex.txt` and `app/fixtures/discrepancy_apex.json`. Apex Distribution
and the displayed order are demo data, not a customer document. The visible replay
banner, `Needs Review`, `PriceMismatch`, and `CatalogMatchingMismatch` are application
output. Approval was checked to be disabled; it is below the captured viewport.

There was no response interception, fixture alteration, DOM/style manipulation,
warning removal, or AI image generation. PNG was converted to lossless WebP and
decoded RGB pixels were checked for equality. This demonstrates the replay UI and
deterministic workflow, not live-model quality, a production deployment, or approval
of the displayed order.
