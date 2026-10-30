"""Mutation check: each mutant removes one gate; the security suite must fail for every mutant.
Run from app/: python3 -B tests/mutation_check.py OUT.json
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MUTANTS = {
    "M1_execute_without_verified_capture": ("autonomos/service.py",
        'if not cap or cap["verification"].get("verdict") != "CAPTURE_VERIFIED":', "if False:"),
    "M2_skip_amount_check": ("autonomos/payment_verifier.py",
        'return a["currency_code"] == currency and Decimal(a["value"]) == Decimal(value)', "return True"),
    "M3_skip_task_binding": ("autonomos/payment_verifier.py",
        'pu.get("reference_id") == offer["task_id"]', "True"),
    "M4_no_execution_uniqueness": ("autonomos/store.py",
        "task_id TEXT UNIQUE NOT NULL REFERENCES tasks, started REAL", "task_id TEXT NOT NULL REFERENCES tasks, started REAL"),
    "M5_webhook_replay_allowed": ("autonomos/store.py",
        "CREATE TABLE IF NOT EXISTS webhooks (transmission_id TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,",
        "CREATE TABLE IF NOT EXISTS webhooks (wid INTEGER PRIMARY KEY, transmission_id TEXT, event_id TEXT NOT NULL,"),
    "M6_unverified_webhook_accepted": ("autonomos/service.py",
        'if v.get("verification_status") != "SUCCESS":', "if False:"),
    "M7_model_output_keys_unchecked": ("autonomos/research.py",
        "bad_keys = sorted(_keys(rep) & FORBIDDEN_OUTPUT_KEYS)", "bad_keys = []"),
    "M8_quote_not_checked": ("autonomos/research.py",
        "elif not docset.quote_in_passage(pid, quote):", "elif False:"),
    "M9_offer_digest_not_required": ("autonomos/service.py",
        'if offer_digest_seen != offer["_digest"]:', "if False:"),
    "M10_trust_provider_without_get": ("autonomos/service.py",
        "return self._verify_and_gate(tid, offer, order, cap_resp=resp, truth=truth)",
        "return self._verify_and_gate(tid, offer, order, cap_resp=resp, truth=resp)"),
    # ---- R1 P0 mutants
    "R1_busy_lease_ignored": ("autonomos/store.py",
        'if a["owner"] != self.owner and a["lease_until"] > now:', "if False:"),
    "R1_rowcount_not_checked": ("autonomos/store.py",
        "        if n != 1:\n            raise ConcurrencyError", "        if False:\n            raise ConcurrencyError"),
    "R1_offer_claim_ignored": ("autonomos/service.py",
        'if not self.store.accept_offer(offer["offer_id"]):', 'if not (self.store.accept_offer(offer["offer_id"]) or True):'),
    "R1_decline_is_terminal": ("autonomos/service.py",
        'if e.issue in BUYER_ACTION_ISSUES:', 'if False:'),
    "R1_crashed_execution_rerun": ("autonomos/store.py",
        "self._one(\"UPDATE executions SET status='ABANDONED' WHERE execution_id=? AND status='STARTED'\", (eid,),",
        "self.db.execute(\"DELETE FROM executions WHERE execution_id=? AND ?=?\", (eid, 1, 1)); self._one(\"SELECT 1\", (),"),
    # ---- R1b mutants (uncertain capture, provider errors, webhooks, browser security, owner gate)
    "R1b_recovery_resends_in_flight_capture": ("autonomos/service.py",
        'return self._park_uncertain(tid, oid, truth, "capture outcome still unknown after recovery check")',
        'return self._send_capture_once(tid, offer, order, att, ("SENT", "OUTCOME_UNKNOWN"))'),
    "R1b_recovery_sends_first_capture": ("autonomos/service.py",
        'report["captures"].append((tid, self._capture_with_reservation(tid, may_send=False)))',
        'report["captures"].append((tid, self._capture_with_reservation(tid, may_send=True)))'),
    "R1b_absence_of_capture_is_failure": ("autonomos/service.py",
        """            if truth.get("status") == "VOIDED":
                self.store.capture_mark(oid, ("OUTCOME_UNKNOWN",), "RELEASED", error="PROVIDER_ORDER_VOIDED")""",
        """            if not self._has_capture(truth):
                self.store.capture_mark(oid, ("OUTCOME_UNKNOWN",), "RELEASED", error="PROVIDER_ORDER_VOIDED")"""),
    "R1b_ambiguous_429_is_definitive": ("autonomos/payments/mock.py",
        "return 400 <= self.status < 500 and self.status not in (401, 408, 409, 429) \\",
        "return 400 <= self.status < 500 and self.status not in (401, 408, 409) \\"),
    "R1b_settle_delay_not_enforced": ("autonomos/service.py",
        "if age < self.uncertain_settle_s:", "if False:"),
    "R1b_idempotency_window_not_enforced": ("autonomos/service.py",
        "if age > self.idempotency_window_s:", "if False:"),
    "R1b_new_generation_without_reapproval": ("autonomos/store.py",
        'if not a["reapproval"]:', "if False:"),
    "R1b_adapter_resends_mutation_on_401": ("autonomos/payments/paypal_sandbox.py",
        "                if mutation:", "                if False:"),
    "R1b_ack_without_persistence": ("autonomos/service.py",
        """        except sqlite3.Error:
            return {"accepted": False, "http": 503,""",
        """        except sqlite3.Error:
            return {"accepted": True, "http": 200,"""),
    "R1b_queued_webhooks_not_recovered": ("autonomos/store.py",
        "\"OR outcome='queued' ORDER BY received\"", "\"AND 0 ORDER BY received\""),
    "R1b_csrf_not_checked": ("autonomos/server.py",
        'if not had_cookie or not hmac.compare_digest(f.get("csrf", ""), self._csrf()):', "if False:"),
    "R1b_session_binding_skipped": ("autonomos/server.py",
        'if t is None or t["request"].get("buyer_ref") != self._buyer_ref():', "if False:"),
    "R1b_origin_not_checked": ("autonomos/server.py",
        "            if not self._origin_ok():", "            if False:"),
    "R1b_sandbox_capture_cap_ignored": ("autonomos/service.py",
        "        return n >= self.cfg.sandbox_max_capture_requests", "        return False"),
    "R1b_ai_error_not_mapped": ("autonomos/ai.py",
        """            raise AIError(f"Anthropic API HTTP {e.code} {err} request-id={rid}; not retried") from None""",
        """            raise"""),
    # ---- R1c mutants (cancel-callback integrity, cancel/capture atomicity, cancelled-task recovery, order-status)
    "R1c_redirect_cancels_task": ("autonomos/service.py",
        """        st, a = self.store.task(tid)["state"], self.store.capture_attempt(order_id)""",
        """        self.store.transition(tid, S.CANCELLED, "buyer", "cancellation", {"reason": "x"}, reason="x")
        st, a = self.store.task(tid)["state"], self.store.capture_attempt(order_id)"""),
    "R1c_cancel_blocker_ignored": ("autonomos/store.py",
        """        a = self.capture_attempt(order_id)
        if a:
            if a["state"] in ("SENT", "OUTCOME_UNKNOWN"):""",
        """        a = None
        if a:
            if a["state"] in ("SENT", "OUTCOME_UNKNOWN"):"""),
    "R1c_sent_attempt_not_blocking": ("autonomos/store.py",
        """if a["state"] in ("SENT", "OUTCOME_UNKNOWN"):
                return f"a capture request""",
        """if a["state"] in ("OUTCOME_UNKNOWN",):
                return f"a capture request"""),
    "R1c_unknown_attempt_not_blocking": ("autonomos/store.py",
        """if a["state"] in ("SENT", "OUTCOME_UNKNOWN"):
                return f"a capture request""",
        """if a["state"] in ("SENT",):
                return f"a capture request"""),
    "R1c_resolved_attempt_not_blocking": ("autonomos/store.py",
        """            if a["state"] == "RESOLVED":
                return "a capture is recorded""",
        """            if False:
                return "a capture is recorded"""),
    "R1c_live_reservation_not_blocking": ("autonomos/store.py",
        """if a["state"] == "RESERVED" and a["lease_until"] > now:""", "if False:"),
    "R1c_cancel_txn_guard_skipped": ("autonomos/service.py",
        """                b = o and self.store.cancel_blocker(o["order_id"])""", """                b = None"""),
    "R1c_cancel_without_provider_read": ("autonomos/service.py",
        """                    truth = self._get_order(oid)               # authenticated, server-side""",
        """                    truth = {"status": "PAYER_ACTION_REQUIRED"}"""),
    "R1c_write_ahead_ignores_task_state": ("autonomos/store.py",
        """AND state IN (%s) AND (?=0 OR EXISTS""", """AND state IN (%s) AND (?=0 OR 1 OR EXISTS"""),
    "R1c_reserve_on_closed_task": ("autonomos/store.py",
        """            if self._order_task_state(order_id) not in S.OPEN_FOR_CAPTURE:""", """            if False:"""),
    "R1c_cancelled_task_executes": ("autonomos/service.py",
        """            if self.store.was_cancelled(tid):""", """            if False:"""),
    "R1c_reopen_edge_any_actor": ("autonomos/states.py",
        """    return rule is None or actor in rule[0]""", """    return True"""),
    "R1c_reopen_edge_any_evidence": ("autonomos/store.py",
        """            if rule and (evidence_kind != rule[1] or evidence is None):""", """            if False:"""),
    "R1c_reconcile_without_provider_capture": ("autonomos/service.py",
        """        if not self._has_capture(truth):
            return {"task_id": tid, "state": S.CANCELLED, "changed": False""",
        """        if False:
            return {"task_id": tid, "state": S.CANCELLED, "changed": False"""),
    "R1c_order_status_db_not_immutable": ("autonomos/order_status.py",
        '    q = "?mode=ro" if os.path.exists(path + "-wal") else "?mode=ro&immutable=1"', '    q = "?mode=ro"'),
    "R1c_order_status_db_writable": ("autonomos/order_status.py",
        '    q = "?mode=ro" if os.path.exists(path + "-wal") else "?mode=ro&immutable=1"', '    q = "?mode=rwc"'),
    "R1c_order_status_full_provider_surface": ("autonomos/order_status.py",
        """    @property
    def environment(self):
        return self._provider.environment""",
        """    def __getattr__(self, name):
        return getattr(self._provider, name)

    @property
    def environment(self):
        return self._provider.environment"""),
    "R1c_order_status_unmasked": ("autonomos/order_status.py",
        """    return (x[:4] + "…" + x[-4:]) if isinstance(x, str) and len(x) > 10 else x""", """    return x"""),
    "R1c_recover_hides_cancelled_capture": ("autonomos/service.py",
        """            report["cancelled_with_capture_attempt"].append((tid, ast))""", """            pass"""),
}


def run(name):
    tmp = tempfile.mkdtemp(prefix="autonomos_mut_")
    shutil.copytree(os.path.join(APP, "autonomos"), os.path.join(tmp, "autonomos"))
    shutil.copytree(os.path.join(APP, "tests"), os.path.join(tmp, "tests"))
    os.symlink(os.path.join(APP, "corpus"), os.path.join(tmp, "corpus"))
    os.symlink(os.path.join(APP, "recordings"), os.path.join(tmp, "recordings"))           # R1e
    if name in MUTANTS:
        f, old, new = MUTANTS[name]
        p = os.path.join(tmp, f)
        s = open(p).read()
        if s.count(old) != 1:
            return {"error": f"anchor found {s.count(old)} times"}
        open(p, "w").write(s.replace(old, new))
    r = subprocess.run([sys.executable, "-B", "-W", "ignore", "-m", "unittest", "discover", "-s", "tests"], cwd=tmp,
                       capture_output=True, text=True, timeout=600, env=dict(os.environ, AUTONOMOS_AI_PROVIDER="fixture",
                                                                              AUTONOMOS_PAYMENT_PROVIDER="mock",
                                                                              AUTONOMOS_R1E_LAUNCHER=os.path.join(
                                                                                  APP, "..", "live_demo",
                                                                                  "owner_run_unified_demo.sh")))
    shutil.rmtree(tmp, ignore_errors=True)
    fails = [l for l in r.stderr.splitlines() if l.startswith(("FAIL:", "ERROR:"))]
    return {"killed": r.returncode != 0, "failing": fails[:6], "tail": r.stderr.strip().splitlines()[-1:]}


if __name__ == "__main__":
    res = {"M0_control": run("M0_control")}
    for m in MUTANTS:
        res[m] = run(m)
        print(m, res[m].get("killed"), res[m].get("error"), res[m].get("failing", [])[:2])
    out = {"control_passes": not res["M0_control"]["killed"],
           "killed": sum(1 for k, v in res.items() if k != "M0_control" and v.get("killed")), "mutants": len(MUTANTS),
           "results": res}
    json.dump(out, open(sys.argv[1], "w"), indent=1)
    print("control passes:", out["control_passes"], "killed", out["killed"], "/", out["mutants"])
