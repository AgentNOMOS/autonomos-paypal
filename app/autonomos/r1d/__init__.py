"""AutoNOMOS R1d: governed AI research execution.

R1d adds, without changing any R1c payment code:
  envelope.py   model prices, the cost/token/request envelope and the owner's authorization for REAL execution
  ledger.py     durable, write-ahead record of every model request (cross-process, crash-safe, never retried)
  providers.py  FIXTURE (local rules), MOCK (Messages-API code path over an injected transport) and REAL
                (Anthropic Messages API over HTTPS) providers; one request = one HTTP call, no tools, no fallbacks
  engine.py     the bounded research worker: deterministic retrieval, one structured model request
  verifier.py   deterministic evidence verification, independent of the model
  receipt2.py   receipt v2 (v1 body + an `ai_execution` section); verifies v1 receipts unchanged
  service.py    R1dService: AutoNomos with the R1d execute() and recover()
  aiconfig.py   provider + envelope selection from the environment
  preflight.py  read-only check whether a REAL provider could be enabled safely (no network)

Execution classes, recorded in every artifact and receipt:
  FIXTURE_EXECUTION     deterministic local rules. Not a model.
  MOCK_EXECUTION        the real provider's request/response code over a test or emulated transport. Not a model.
  REAL_MODEL_EXECUTION  an HTTPS request to api.anthropic.com was made under an owner authorization.
"""
FIXTURE_EXECUTION = "FIXTURE_EXECUTION"
MOCK_EXECUTION = "MOCK_EXECUTION"
REAL_MODEL_EXECUTION = "REAL_MODEL_EXECUTION"
EXECUTION_CLASSES = (FIXTURE_EXECUTION, MOCK_EXECUTION, REAL_MODEL_EXECUTION)
