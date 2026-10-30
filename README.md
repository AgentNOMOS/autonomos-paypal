# AutoNOMOS: paid AI research you can check

**PayPal AI Hackathon 2026 submission.** A buyer pays through **PayPal (Sandbox)** for one research question about an approved document set. Only after the server has captured the payment **and verified PayPal's own order record** does AutoNOMOS send **one** governed request to **Claude Opus 5.5**. Every finding must quote the documents verbatim and pass deterministic support checks. The buyer gets the report plus a **signed receipt** that chains offer → payment → execution → delivery.

> **Scope and honesty notes (read first)**
> - **Sandbox only.** PayPal Sandbox moves test money. This is not a production payment service, not escrow, and makes no refund or buyer-protection promise. Live PayPal hosts are refused in code.
> - **"Document-supported" is not "true".** The checker confirms that a claim is supported by the quoted passage under fixed English rules. It never asserts scientific truth, and it is not perfect: on our untuned held-out set, about 1 in 9 deliberately wrong non-numeric conclusions still passed (2/18).
> - **Receipts use a local demo Ed25519 key.** A receipt proves the records were not altered after signing. It does not prove PayPal's origin of the payment data; re-query PayPal for that.
> - **The sample documents are about a quantum-computing experiment.** Their hardware results are *reported by their authors*; AutoNOMOS did not verify them.

## What has actually been run

| Run | Date | What it shows | Evidence |
|---|---|---|---|
| PayPal Sandbox Run 2 | 2026-10-16 | 1 Sandbox order, exactly 1 capture POST, 1.00 USD test money, `CAPTURE_VERIFIED` 9/9 from PayPal's order record, execution 7 ms *after* verification, receipt valid. AI was the labelled rules-only fixture. | author's sealed evidence |
| Real AI Test 1 | 2026-10-17 | 1 real Claude Opus 5.5 request (`req_011Cg7ZywMA4FMtZQC9G2QbX`), 5,975 in / 2,152 out tokens, about 0.07 USD, parsed, verified, receipt valid. Payment was MOCK. | `app/recordings/real_ai_test1_opus55/`, `app/tests/fixtures/real_ai_test1/` |
| **Combined live run** (Sandbox payment + real Claude) | 2026-10-30 | 1 Sandbox order (1.00 USD test money), exactly 1 capture request, `CAPTURE_VERIFIED` 9/9 from PayPal's order record; execution authorized only after that; 1 real Claude Opus 5.5 request (`req_011CgZ1yhXaPsmUvW3V3cs1u`), 5,882 in / 1,849 out tokens, about 0.07 USD; research **PARTIAL** (4 of 5 findings document-supported, 1 unconfirmed; truth not established); receipt valid 15/15. An earlier attempt that evening was cancelled at PayPal checkout: 0 captures, 0 AI requests. | `docs/live_run/` |

## Quick start for judges (no accounts, no keys, no network)

Requires Python 3.10+ (tested on 3.12). **No third-party packages.**

```bash
git clone https://github.com/AgentNOMOS/autonomos-paypal.git autonomos && cd autonomos/app

# 1. Test suite: about 75 s. Expect "Ran 346 tests ... OK (skipped=4)".
#    The 4 skips replay the author's private Sandbox incident database and sealed Sandbox Run 2 evidence,
#    which are not part of this repository.
python3 -m unittest discover -s tests

# 2. Headless end-to-end run: MOCK payment + the REPLAYED real Claude Opus 5.5 answer
python3 -m autonomos demo /tmp/autonomos-demo --ai recorded

# 3. Verify the receipt offline, re-checking every quotation against the documents
python3 -m autonomos verify-receipt /tmp/autonomos-demo/receipt.json \
  /tmp/autonomos-demo/artifact.json /tmp/autonomos-demo/events.json \
  --corpus corpus/agentnomos_quantum
```

### Browser demo (replay, offline)

```bash
cd app
AUTONOMOS_DEMO_MODE=DEMO_REPLAY AUTONOMOS_PAYMENT_PROVIDER=mock AUTONOMOS_R1D_AI_PROVIDER=recorded \
AUTONOMOS_DATA_DIR=/tmp/autonomos-replay python3 -m autonomos serve
# open http://127.0.0.1:8765  (loopback only)
```

Every page carries a purple **MODE DEMO_REPLAY** banner: MOCK payment (no PayPal) and a *replayed* Claude answer, "not a live model call". The question is fixed, because the replay answers only the recorded question. You will see the offer, the mock checkout, the status panel (payment, execution authorization *after* the verified capture, AI real vs replayed, document-supported vs unconfirmed findings, **Independent scientific truth: NOT ESTABLISHED**, signed receipt, receipt verifier) and the report.

The offer text says "PayPal Sandbox only" even in MOCK mode, because the signed offer terms are identical across modes. The MOCK banner on every page states what is actually happening.

## Real PayPal Sandbox checkout with your own free Sandbox app

You need a free PayPal developer account at <https://developer.paypal.com/>:
- a **Sandbox REST app** (client id + secret);
- the Sandbox **business** account's merchant/account id (the payee);
- a Sandbox **personal** (buyer) account to pay with.

No Anthropic key is needed: the AI step uses the same labelled replay of the real Claude answer.

```bash
# keep credentials in a private file OUTSIDE the repository
cat > ~/autonomos-sandbox.env <<'EOF'
AUTONOMOS_PAYMENT_PROVIDER=paypal_sandbox
PAYPAL_CLIENT_ID=your-sandbox-client-id
PAYPAL_CLIENT_SECRET=your-sandbox-secret
PAYPAL_MERCHANT_ID=your-sandbox-business-account-id
EOF
chmod 600 ~/autonomos-sandbox.env

cd app
set -a; . ~/autonomos-sandbox.env; set +a
export AUTONOMOS_ENV_FILE=~/autonomos-sandbox.env
export AUTONOMOS_SANDBOX_OWNER_GO=capture-at-most-1-order   # explicit opt-in; credentials alone are not permission
export AUTONOMOS_R1D_AI_PROVIDER=recorded
export AUTONOMOS_DATA_DIR=/tmp/autonomos-sandbox-1          # hard cap: ONE Sandbox order per data directory
python3 -m autonomos sandbox-preflight                      # offline check, prints no secrets
python3 -m autonomos serve                                  # http://127.0.0.1:8765
```

- **Keep the pre-filled question.** The replay answers only that question. If you change it, the payment still completes (test money), the replay refuses, and the task goes to `REVIEW_REQUIRED` with no receipt. That is the designed behaviour for paid work that fails.
- The banner reads "DEVELOPMENT configuration (not a demo mode): payment paypal_sandbox, AI MOCK_EXECUTION". That is accurate: real Sandbox payment, replayed AI.
- For another checkout, use a new `AUTONOMOS_DATA_DIR`.

## How it works

```
question → capability check → fixed-price offer (buyer confirms its digest)
  → PayPal Orders v2: create order → buyer approves at PayPal
  → server-side capture (write-ahead, at most one POST, PayPal-Request-Id)
  → GET /v2/checkout/orders/{id}: 9 checks (amount, currency, payee, task + offer binding, exactly one capture …)
  → CAPTURE_VERIFIED → EXECUTION_AUTHORIZED
  → ONE Claude Opus 5.5 request (ledger row written BEFORE sending; never retried; unknown outcome booked at worst case)
  → every finding: verbatim quote + same-passage context + numeric and meaning rules
  → document-supported findings / unconfirmed or withheld conclusions, delivered separately
  → signed receipt (Ed25519 demo key) chaining every event
```

**Payment-safety properties, each covered by tests:**
- no AI before a verified capture (unapproved, pending, declined, unknown and forged states);
- never twice after a crash, an uncertain response or concurrent executes;
- duplicate clicks give one order and one execution;
- an ambiguous capture response is reconciled **by GET only**, never by re-sending automatically;
- the model's output cannot change price, payee or payment state;
- webhooks never authorize anything (they are off in the demos).

The suite has 346 tests (security, state machine, multi-process races, crash recovery, evidence rules, browser flow, launcher), plus targeted mutation testing.

| Path | Content |
|---|---|
| `app/autonomos/service.py`, `states.py` | the governor: 13 states, transition table, evidence and actor gates |
| `app/autonomos/payments/paypal_sandbox.py` | PayPal Orders v2 over stdlib HTTPS (Sandbox host only) |
| `app/autonomos/payment_verifier.py` | verdicts from PayPal's own order record |
| `app/autonomos/r1d/` | governed Anthropic client, write-ahead ledger, authorization envelope, research engine, evidence verifier, receipt v2 |
| `app/autonomos/server.py` | loopback web UI and status panel |
| `app/corpus/agentnomos_quantum/` | the approved documents: unmodified public MIT documentation, pinned by commit (`SOURCES.json`, `licenses/`) |
| `app/recordings/` | the recorded real Claude answer used by the replay modes |
| `live_demo/` | the author's single-use launcher for the supervised live run (real Sandbox + real Claude); see below |

## Real Claude requests (billed to your own key; optional)

A real model request needs all of the following:
- `AUTONOMOS_R1D_AI_PROVIDER=anthropic_real`;
- an API key **file** outside the repository (mode 0600), named by `AUTONOMOS_ANTHROPIC_KEY_FILE`; `ANTHROPIC_API_KEY` must be **unset**;
- a signed-off authorization JSON (model, token and USD caps, allowed payment environments, data directory, expiry), named by `AUTONOMOS_R1D_AI_AUTHORIZATION`;
- `AUTONOMOS_R1D_REAL_AI_GO=real-ai:<authorization_id>`.

`python3 -m autonomos ai-preflight` shows what is missing and the worst-case cost, without network and without revealing the key. The variables are documented at the top of `app/autonomos/r1d/aiconfig.py`, and an example authorization is in `live_demo/ai_authorization_TEMPLATE_r1e1_live1.json`. The author's combined live run used `live_demo/owner_run_unified_demo.sh`, which also enforces one Sandbox order, one capture POST, one model request and an interactive terminal.

## Limits

- Sandbox capture = test money. A COMPLETED capture can still be reversed.
- The evidence checker uses finite English vocabularies; lexical coverage is not entailment. Known residual false supports include "identical" → "a superset", and "not A or B" → "not A, only B".
- The in-page receipt verifier uses the server's own key. Independent checking means the `verify-receipt` CLI with a published key.
- The Ed25519 code is the RFC 8032 reference algorithm in pure Python (test vector checked), meant for a demo key, not production signing.
- Loopback only; no public webhook listener; no deployment.

## Provenance

All AutoNOMOS code was written from 2026-10-15, inside the hackathon submission period, with AI coding assistance. The document set is unmodified public MIT documentation from three earlier AgentNOMOS repositories by the same author, used as input data only (see `app/corpus/agentnomos_quantum/SOURCES.json`).

## License

MIT, see `LICENSE`. Corpus files keep their own MIT licences in `app/corpus/agentnomos_quantum/licenses/`.
