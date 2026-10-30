# Quantum Claim Court

**Where scientific claims face literature, mathematics and quantum evidence.**
A Qollab × IonQ Quantum Hackathon 2026 project by AgentNOMOS Quantum Lab. MIT licensed.

> The project does not invent quantum cloning. It creates an auditable scientific claim-verification experience around established quantum physics.

Eighteen claims about copying quantum states go on trial. Each is examined by three independent witnesses:
- the **original literature**, at the level actually checked: metadata, abstract or title, never full text;
- **mathematics**: the no-cloning theorem, the 5/6 optimum, and the exact values of this circuit;
- one **real quantum measurement**: a 3-qubit Bužek–Braunstein–Hillery–Bruß universal cloner run once per Run on Qollab, 2000 shots, on the built-in simulator or IonQ's free remote simulator.

Verdicts: Supported, Disputed, Insufficient, Rewrite, Blocked.

## Repository layout

| Path | Contents |
|---|---|
| `app_r3b/` | The R3b app (IonQ free-simulator compatible): `app/` plus a backend policy, runner and R3b verifier. See `docs/R3B_IONQ_COMPATIBILITY.md` |
| `app/` | The R2 interactive Qollab app: `index.html`, `main.js`, `main.css`, `ui/`, the Python bridge `uqcc_app/`, the R2 verifier, and the README shown on Qollab |
| `qollab_r1/` | The frozen R1 science package (`uqcc_r1/`: circuit, interpretation, claims, source manifest), the R1 verifier and the R1 smoke entrypoint |
| `uqcc/`, `verifier/verify_uqcc.py`, `results/` | R0 research spike: 16-claim docket over 8 input states, with its own verifier |
| `scripts/` | Deterministic builds (`build_r3b_zip.py`, `build_r2_zip.py`, `build_r1_zip.py`), pinned hashes (`frozen_pins.json`, `r3b_pins.json`), `local_run.py`, `exact_reference.py`, `ionq_offline_check.py`, manifest generators |
| `evidence/` | Raw Crossref/arXiv responses from 2026-10-10 (see `evidence/README.md`) |
| `tests/`, `dev/` | Python, browser (headless Chromium, mocked bridge) and JS tests; fixtures from the real bridge |
| `docs/` | `REPRODUCIBILITY.md`, `submission/` (references, submission text, video script), round reports, `PROVENANCE.md` |

## Quick start

    python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/python scripts/build_r3b_zip.py     # R3b Qollab ZIP, IonQ-simulator compatible (pinned in scripts/r3b_pins.json)
    .venv/bin/python scripts/build_r2_zip.py      # R2 Qollab ZIP, byte-identical (sha256 8edd6166…9cf16ea0)
    .venv/bin/python scripts/local_run.py out/    # one local run (new counts each time)
    python3 -I scripts/exact_reference.py         # exact 2/3, 1/6, 1/6, 0 (stdlib only)
    .venv/bin/python -m pytest -q                 # set UQCC_CHROMIUM for the browser tests

**Public Qollab project:** https://qollab.xyz/u/agentnomos/quantum-claim-court (open it, select the built-in simulator, press Run).

**To run your own copy on Qollab:** upload `dist/uqcc_r3b_ionq_compatible_rc.zip` as a Qollab project, select the built-in simulator or a free IonQ remote simulator, and press Run once. IonQ quantum computers (hardware, credits) are refused by the app.

**IonQ status:** a Run attempted on IonQ's free remote simulator in Qollab failed with a backend connection error. No IonQ result exists or is claimed; the documented results come from Qollab's built-in ideal simulator.

## Limitations

- A simulator, not hardware. The built-in simulator is ideal. IonQ remote simulators apply a noise model, so their results may fall below the ideal exact values. That describes the noisy Run, not the mathematics: noise cannot disprove the no-cloning theorem.
- One input state is measured live.
- 5/6 is optimal only for deterministic, symmetric, universal 1→2 qubit cloning.
- Finite shots: about 1.4% of correct runs show "Disputed" by chance.
- No full-text review of the literature.
- The claim register is frozen and hand-structured. This is **not** a general-purpose fact checker.
- SHA-256 shows that content is unchanged, not where it came from.
- Approximate cloning of a known theoretical optimum, not perfect cloning and not a new theorem. No quantum advantage is claimed.
- The R3b app accepts only Qollab's built-in BasicSimulator and the free IonQ remote simulator, positively identified as qiskit-ionq's `IonQSimulatorBackend`. Anything else is refused before any job is sent. The IonQ path is tested with synthetic doubles and offline interface checks; a live IonQ-simulator Run is the owner's acceptance step. The R2 app (`app/`) accepts BasicSimulator only.

## Prior work and AI assistance

- **Prior work:** the team has earlier concepts for verifying scientific claims (UVO / ActionGate 2.0). They inspired the separation into evidence classes. No code, data or private API from them was used. This is a clean-room implementation written during the hackathon build window.
- **AI assistance:** code and text were prepared with AI assistance (Claude, Anthropic) under the team's direction and review.

**This effort is supported by Qollab & IonQ.**
