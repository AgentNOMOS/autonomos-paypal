"""Drive the local UI end to end (MOCK payments, configured AI provider) and take screenshots with a headless browser.
usage (from app/): AUTONOMOS_AI_PROVIDER=claude_cli python3 -B tests/capture_screens.py OUT_DIR CHROME_HEADLESS_SHELL
"""
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from autonomos.config import Config  # noqa: E402
from autonomos.server import make_handler  # noqa: E402
from autonomos.service import AutoNomos  # noqa: E402

out, chrome = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=False)
httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
port = httpd.server_address[1]
base = f"http://127.0.0.1:{port}"
svc = AutoNomos(Config.from_env(data_dir=os.path.join(out, "var"), payment_provider="mock", public_base_url=base))
httpd.RequestHandlerClass = make_handler(svc)
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def shot(name, url, h=1400):
    subprocess.run([chrome, "--no-sandbox", "--hide-scrollbars", f"--window-size=1000,{h}", f"--screenshot={out}/{name}.png",
                    url], capture_output=True, timeout=60)
    print(name, os.path.exists(f"{out}/{name}.png"))


def post(path, data):
    return urllib.request.urlopen(urllib.request.Request(base + path, data=data.encode(), method="POST"))


shot("1_request", base + "/", 900)
q = "What hardware evidence exists for the quantum-kernel experiment, and what is still unverified?"
r = post("/tasks", "capability=research_snapshot&corpus=agentnomos_quantum&topic=" + urllib.parse.quote(q))
tid = r.geturl().rsplit("/", 1)[1]
shot("2_offer_review", f"{base}/tasks/{tid}", 1100)
r = post(f"/tasks/{tid}/accept", "offer_digest=" + svc.store.offer(task_id=tid)["_digest"])
oid = r.geturl().rsplit("/", 1)[1]
shot("3_checkout_mock", r.geturl(), 700)
post(f"/mock-paypal/{oid}/approve", "")
for _ in range(600):
    if svc.store.task(tid)["state"] in ("DELIVERED", "REVIEW_REQUIRED", "FAILED"):
        break
    time.sleep(0.5)
print("final state", svc.store.task(tid)["state"])
shot("4_delivered_report_and_receipt", f"{base}/tasks/{tid}", 3400)
open(f"{out}/task_id.txt", "w").write(tid)
httpd.shutdown()
