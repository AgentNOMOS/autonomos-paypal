"""Child process for the R1e crash test. Usage: python3 r1e_child.py '<json>'
json: {"data_dir", "tid", "log": path, "mode": "crash"}. Opens the SHARED database with its own service and the
RECORDED replay transport; in "crash" mode the transport logs the request and the process exits (os._exit(9)) before
any response is read - a crash after the ledger's write-ahead SENT mark.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r1d_support import new_service  # noqa: E402
from autonomos.r1d.recorded import recording_runtime  # noqa: E402

a = json.loads(sys.argv[1])
rt = recording_runtime(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "recordings",
                                    "real_ai_test1_opus55"))
inner = rt.provider._open


def crash(req, timeout=0):
    with open(a["log"], "a") as f:
        f.write(f"{os.getpid()} {time.time()}\n")
    os._exit(9)


rt.provider._open = crash
svc = new_service(rt, data_dir=a["data_dir"])
print(json.dumps({"state": svc.execute(a["tid"])}))
