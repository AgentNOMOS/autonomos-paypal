"""Child process for the R1d crash and concurrency tests. Usage: python3 r1d_child.py '<json>'
json: {"data_dir", "tid", "step": "honest" | "exit" | "slow", "log": path, "start_at": epoch, "envelope": {...}}
Opens the SHARED database with its own R1dService and calls execute(tid) once. A mock transport logs every request.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r1d_support import FakeMessagesAPI, honest_answer, mock_runtime, new_service  # noqa: E402
from autonomos.service import GateError  # noqa: E402

a = json.loads(sys.argv[1])
step = {"honest": ("answer", honest_answer), "exit": ("exit", 9),
        "slow": ("sleep", 0.6, ("answer", honest_answer))}[a["step"]]
rt, api = mock_runtime(FakeMessagesAPI(step, log_path=a["log"]), **a.get("envelope", {}))
svc = new_service(rt, data_dir=a["data_dir"])
while time.time() < a.get("start_at", 0):
    time.sleep(0.005)
try:
    st = svc.execute(a["tid"])
    print(json.dumps({"pid": os.getpid(), "state": st}))
except GateError as e:
    print(json.dumps({"pid": os.getpid(), "refused": str(e)}))
