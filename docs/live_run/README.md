# Combined live run, 2026-10-30: one PayPal Sandbox payment, one real Claude Opus 5.5 request

The author ran this as a supervised run on a loopback server with release candidate r1e1, the code in this repository.

1. The buyer kept the default research question. The server returned a fixed offer of **1.00 USD**, paid in PayPal **Sandbox** test money.
2. The buyer approved the payment once in PayPal's Sandbox checkout. The server sent **one** capture request, then read PayPal's own order record back with `GET /v2/checkout/orders/{id}`. It passed all **9 of 9** verification checks: `CAPTURE_VERIFIED`.
3. **Only after** that verification did the server authorize execution. It then sent **one** request to the Anthropic Messages API (`claude-opus-5-5`, effort medium).
4. Every finding was checked against the pinned documents. The report was delivered with a signed receipt, which verifies offline.

| Fact | Value |
|---|---|
| PayPal environment | Sandbox (test money; no real funds moved) |
| Orders / capture requests | 1 / 1, capture `COMPLETED`, verification `CAPTURE_VERIFIED` 9/9 |
| Order id / capture id (masked) | `2YN2…804X` / `6WW2…2250` |
| Claude requests | 1, `claude-opus-5-5`, request id `req_011CgZ1yhXaPsmUvW3V3cs1u`, stop reason `end_turn` |
| Tokens | 5,882 input / 1,849 output (limits 34,000 / 12,000) |
| AI cost | 0.0666 USD booked at our conservative internal rates (4.40/22.00 USD per million tokens); ≈ 0.061 USD at list price. The ceiling was 0.50 USD, and 0.34 USD worst case was reserved before sending. |
| Timeline (UTC) | order 20:00:24 → capture verified and execution authorized 20:01:42 → AI request → delivered 20:01:59 |
| Research result | **PARTIAL**: 4 of 5 findings document-supported, 1 unconfirmed (rule `R1E1-SUPPORT-1`) |
| Independent scientific truth | **Not established (0/5).** The checker verifies support by quotation, not truth. |
| Receipt | valid, **15/15** offline checks, signed with a local demo Ed25519 key generated for this run |

`execution_summary.json` and `verify_receipt.json` in this folder are copied unchanged from the sealed evidence. The raw receipt, database and authorization files stay private, because they contain unmasked PayPal identifiers.

## Honest notes
- **An earlier attempt the same evening was cancelled at PayPal checkout:** the wrong Sandbox account was used and the buyer pressed Cancel. Its record shows 1 order, **0 captures and 0 AI requests**, which is the payment gate working as designed.
- **The successful run used a separate, freshly issued authorization, data directory and single-use marker.** For this, the author's launcher used its own path-override settings. The launcher documents those settings for offline rehearsal; using them for a live run was an explicit owner decision, and no code was changed.
- Sandbox only: no revenue, no refunds or escrow, no production deployment, and no external security audit.
