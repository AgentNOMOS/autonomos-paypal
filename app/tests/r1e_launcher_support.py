"""Fake owner files for offline rehearsals of the R1e unified-demo launcher. Nothing here is a real credential.
The launcher path comes from AUTONOMOS_R1E_LAUNCHER (set by the mutation runner, whose sandbox has no live_demo/)
or defaults to ../live_demo next to app/. A missing launcher is a test FAILURE, never a skip (R1e item 11)."""
import json
import os
import socket
import tempfile
import time

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LAUNCHER = os.environ.get("AUTONOMOS_R1E_LAUNCHER") or os.path.abspath(
    os.path.join(APP, "..", "live_demo", "owner_run_unified_demo.sh"))
FAKE_PP_SECRET = "FAKE-PAYPAL-SECRET-TESTONLY-0123456789abcdef"
FAKE_PP_CLIENT = "FAKE-PAYPAL-CLIENT-TESTONLY-abcdef0123456789"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, text.encode())
    os.close(fd)


def owner_files(key, payment_envs=("SANDBOX",), **auth_over):
    """(dir, launcher env) for one rehearsal: key file, PayPal env file and authorization, all 0600, all fake."""
    d = tempfile.mkdtemp(prefix="r1e_owner_")
    out = os.path.join(d, "out")
    a = {"authorization_id": "AUTH-R1E-REHEARSAL", "mode": "REAL_MODEL_EXECUTION", "provider": "anthropic_messages",
         "model": "claude-opus-5-5", "effort": "medium", "max_executions": 1, "max_requests_total": 1,
         "max_requests_per_execution": 1, "max_input_tokens_per_request": 34000, "max_output_tokens_per_request": 12000,
         "max_total_tokens": 46000, "max_usd_total": "0.50", "price_usd_per_mtok": {"input": "4.40", "output": "22.00"},
         "allowed_payment_environments": list(payment_envs), "request_timeout_s": 600,
         "not_after": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)),
         "bound_data_dir": os.path.join(out, "data"),
         "owner_statement": "Offline rehearsal authorization; no request is ever sent by this test."}
    a.update(auth_over)
    write_private(os.path.join(d, "auth.json"), json.dumps(a))
    write_private(os.path.join(d, "anthropic.key"), key + "\n")
    write_private(os.path.join(d, "sandbox.env"), f"PAYPAL_CLIENT_ID={FAKE_PP_CLIENT}\nPAYPAL_CLIENT_SECRET="
                                                  f"{FAKE_PP_SECRET}\nPAYPAL_MERCHANT_ID=FAKEMERCHANT01\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AUTONOMOS_", "ANTHROPIC_", "PAYPAL_"))}
    env.update(OWN=d, AUTH=os.path.join(d, "auth.json"), KEY=os.path.join(d, "anthropic.key"),
               PPENV=os.path.join(d, "sandbox.env"), OUT=out, MARK=os.path.join(d, "out.LAUNCHED"),
               EVD=os.path.join(d, "evidence"), PORT=str(free_port()))
    return d, env
