# Quantum Research Governor
**Governing Quantum Experiments Through Verifiable Evidence**

AgentNOMOS Quantum Lab · Qollab × IonQ Quantum Hackathon 2026 · Theme: Quantum Unlocked ·
**This effort is supported by Qollab & IonQ.** · MIT licence

> A quantum experiment produced measurements. Does the evidence actually justify the proposed conclusion?

Quantum Research Governor (QRG) runs one 7-qubit Qiskit circuit — the **[[4,2,2]] error-detecting
code** with ancilla-measured **ZZZZ** and **XXXX** stabilizers and a labelled synthetic fault — and a
deterministic governor decides whether the measured syndromes justify a precisely stated claim:
**ACCEPT**, **REJECT**, **ESCALATE** or **FREEZE**. A six-scene interactive app (Proposal → Circuit →
Measurements → Independent Witness → Decision → Limits) explains every step, separates LIVE
measurements from LOCAL demonstrations, and exports evidence that anyone can re-verify offline.

## Run it in Qollab
**Public Qollab project (package B):** https://qollab.xyz/u/agentnomos/quantum-research-governor —
open it, choose the built-in simulator and press **Run**. To run your own copy instead:
Upload `qrg_r2_B_zfault_app.zip` (primary: synthetic phase fault) or `qrg_r2_A_clean_app.zip`
(clean-state control), choose the built-in simulator, press **Run**. One Run = one bridge call = one
bare `QuantumCircuit` on the injected backend; no UI control can start another job.
Rebuild the exact ZIPs from this source with `python3 scripts/build_packages_r2.py`:

| package | SHA-256 |
|---|---|
| `qrg_r2_A_clean_app.zip` | `2cb6ddeb2665713384226fbd2dd5d11e73edfe0be8200b423289424e5c6506cd` |
| `qrg_r2_B_zfault_app.zip` | `e726721c002516b5559e8c6a21ecea7c6291712e8412ce72ffcb7bdaf26cab99` |

A Qollab JS/Qiskit project uses the top-level `index.html` + `main.js`; `main.py` is the earlier
console-only (R1) entry point and is kept for reproducibility.

## Run it locally
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                 # science, governor, verifier, packages (+ browser tests if available)
node --test tests/js/*.test.mjs               # UI logic
python3 verifier/verify_qrg.py evidence.json  # verify any copied evidence record (stdlib only)
```
Full instructions: [`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md).

## What was executed
Real Qollab runs (owner-executed, built-in **ideal** simulator, 2000 shots, one recorded job each):
clean state 0/2000 nontrivial syndromes → ACCEPT; synthetic phase fault 413/2000 XXXX and 0/2000
ZZZZ syndromes, one-sided bounds [0.1917, 0.2220] ⊂ [0.15, 0.25] → ACCEPT; both independently
VERIFIED. Details and what is *not* live: [`docs/QOLLAB_LIVE_EVIDENCE.md`](docs/QOLLAB_LIVE_EVIDENCE.md).
No run used IonQ hardware or the IonQ cloud.
These counts are reference observations from those specific runs, not guaranteed outcomes: every new
Run is a fresh random sample, so its counts (and, rarely, its verdict) will generally differ — see
[`docs/REPRODUCIBILITY.md`](docs/REPRODUCIBILITY.md).

## Repository map
| path | content |
|---|---|
| `index.html`, `main.js`, `main.css`, `ui/` | six-scene Qollab app (no third-party JS) |
| `qrg/` | circuit IR, Qiskit runner, independent pure-Python simulator, statistics, evidence, blinding, policy, governor, Qollab flows (`qollab_app.py`, `js_api.py`) |
| `verifier/verify_qrg.py` | standalone standard-library verifier |
| `experiments/` | R0 governance matrix, exact calibration, referee, naive baseline |
| `tests/` | Python, JS and headless-browser tests (`dev/mock` = mocked bridge, `dev/fixtures` = LOCAL fixtures) |
| `scripts/` | deterministic package builders |
| `docs/` | science model, governance policy, audits, live evidence, reproducibility, submission text, references |
| `results/` | local research outputs (not Qollab runs) |

## Limitations
Detection is not correction; logical errors such as XXII are syndrome-silent, so a clean syndrome does
not prove the absence of errors. Ideal simulator ≠ hardware. The synthetic fault probability is known by
construction. Hashes do not authenticate origin; physically plausible forgeries pass physics checks.
Seed support is advertised by the backend but seeded replay was not demonstrated. Statistical acceptance
is conditional on independent shots and the declared fault model. No fault tolerance, fidelity or quantum
advantage is claimed; the governor is deterministic policy software. See
[`packaging/SCIENTIFIC_LIMITATIONS.md`](packaging/SCIENTIFIC_LIMITATIONS.md).

## Prior work, AI use, references
Established science is cited in [`docs/submission/SCIENTIFIC_REFERENCES.md`](docs/submission/SCIENTIFIC_REFERENCES.md)
(Vaidman et al. 1996; Grassl et al. 1997; Gottesman 1996/1997; Knill–Laflamme 1997; Linke et al. 2017;
Takita et al. 2017; Harper–Flammia 2019; Clopper–Pearson 1934; Schuirmann 1987). Built during the
hackathon window with an AI coding assistant (Anthropic Claude via Claude Code) under the team's
direction; clean-room code, no third-party JavaScript; Qiskit (Apache-2.0). See
[`packaging/DISCLOSURE.md`](packaging/DISCLOSURE.md).
