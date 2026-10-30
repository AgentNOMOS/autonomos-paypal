"""The only path from the research worker to a provider. Order of operations for one model request:

  1. estimate the input upper bound (bytes + overhead) and RESERVE in the ledger (authorization + envelope checks
     inside one write transaction; refusal => nothing is sent)
  2. mark SENT (write-ahead), with a lease of timeout + 60 s
  3. one provider.complete() (one HTTP request at most; no retry anywhere)
  4. finish the ledger row: COMPLETED (reported usage), REJECTED (refused before processing) or OUTCOME_UNKNOWN
     (charged at worst case); post-hoc envelope checks may freeze the authorization
"""
import json

from .ledger import LedgerConflict
from .providers import ProviderFailure
from ..canon import digest

LEASE_MARGIN_S = 60.0


class ModelCallFailed(RuntimeError):
    def __init__(self, kind, message, call_id=None, ledger_state=None):
        super().__init__(f"{kind}: {message}")
        self.kind, self.call_id, self.ledger_state = kind, call_id, ledger_state


class GovernedModelClient:
    def __init__(self, provider, envelope, ledger, payment_environment):
        if provider.execution_class != envelope.execution_class:
            raise ValueError(f"provider class {provider.execution_class} does not match the envelope "
                             f"{envelope.execution_class}")
        if getattr(provider, "model", envelope.model) != envelope.model:
            raise ValueError("provider model differs from the authorized model")
        self.provider, self.envelope, self.ledger = provider, envelope, ledger
        self.payment_environment = payment_environment

    def call(self, *, execution_id, task_id, seq, system, user, schema, timeout_s):
        env = self.envelope
        est = env.input_upper_bound(system, user, json.dumps(schema))
        cid = self.ledger.reserve(execution_id=execution_id, task_id=task_id, seq=seq, env=env, est_input_tokens=est,
                                  payment_environment=self.payment_environment)      # raises LedgerRefusal
        body_digest = digest({"model": env.model, "max_tokens": env.max_output_tokens_per_request, "system": system,
                              "user": user, "schema": schema, "effort": env.effort})
        timeout_s = min(float(timeout_s), env.request_timeout_s)
        try:
            self.ledger.mark_sent(cid, body_digest, lease_s=timeout_s + LEASE_MARGIN_S)
        except LedgerConflict as e:
            self.ledger.release_unsent(cid)
            raise ModelCallFailed("NOT_SENT", str(e), cid, "RELEASED_UNSENT") from None
        try:
            rec = self.provider.complete(system, user, schema, max_tokens=env.max_output_tokens_per_request,
                                         timeout_s=timeout_s, request_digest=body_digest)
        except ProviderFailure as e:
            r = e.response or {}
            state = {"NO": "REJECTED", "YES": "COMPLETED"}.get(e.billing, "OUTCOME_UNKNOWN")
            if state == "COMPLETED" and not r.get("usage"):
                state = "OUTCOME_UNKNOWN"
            self._finish(cid, state, usage=r.get("usage"), reported_model=r.get("model"),
                         request_id=e.request_id, response_id=r.get("response_id"), stop_reason=r.get("stop_reason"),
                         response_digest=r.get("response_digest"), error=f"{e.kind}: {e}")
            raise ModelCallFailed(e.kind, str(e), cid, state) from None
        except Exception as e:                    # noqa: BLE001 - anything else after SENT is an unknown outcome
            self._finish(cid, "OUTCOME_UNKNOWN", error=f"{type(e).__name__} in provider")
            raise ModelCallFailed("PROVIDER_EXCEPTION", type(e).__name__, cid, "OUTCOME_UNKNOWN") from None
        charged, anomalies = self._finish(cid, "COMPLETED", usage=rec.get("usage"), reported_model=rec.get("model"),
                                          request_id=rec.get("request_id"), response_id=rec.get("response_id"),
                                          stop_reason=rec.get("stop_reason"),
                                          response_digest=rec.get("response_digest"))
        rec = dict(rec, ledger_call_id=cid, request_digest=body_digest, est_input_tokens_max=est,
                   charged_usd=None if charged is None else str(charged), envelope_anomalies=anomalies)
        if anomalies:
            raise ModelCallFailed("ENVELOPE_ANOMALY", ",".join(anomalies), cid, "COMPLETED")
        return rec

    def _finish(self, cid, state, **kw):
        try:
            return self.ledger.finish(cid, state, self.envelope, **kw)
        except LedgerConflict as e:
            raise ModelCallFailed("LEDGER_CONFLICT", str(e), cid, None) from None
