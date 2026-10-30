# Quantum Kernel Reproducibility Lab

*AgentNOMOS Research Lab · Murat Keskin*

This package reproduces a 9-qubit quantum-kernel experiment that we ran once on IBM Quantum
hardware, and plans a controlled repeat of it on IonQ trapped-ion hardware.

**The IBM result, as reported by the authors.** The raw IBM job files are withheld from this
public release until redistribution rights are confirmed (`WITHHELD_EVIDENCE.md`), so these
values cannot be recomputed from this repository:

| quantity | exact (noise-free) | `ibm_marrakesh`, 2026-08-23 |
|---|---:|---:|
| self-overlap k(x, x) (pair P0) | 1.000000 | **0.006836** (28 / 4096 shots) |
| kernel dynamic range, max-pair − min-pair (P1 − P2) | 0.998951 | 0.003174 |
| dynamic range retained | — | **0.318 %** |
| surviving contrast f, global-depolarising fit | 1 | **0.0048** (95 % CI 0.0033 – 0.0065) |

The circuit had 660 CZ gates and depth 1668 after routing on the heavy-hex lattice. On that
circuit the hardware kernel was close to the maximally mixed floor (1/512 ≈ 0.00195). Rank order
was partly preserved (Spearman ρ = 0.8 on P1–P4), but the exact value lay outside the 95 % CI
for all five entries.

**The question for the proposed work:** if the same kernel workload runs on an all-to-all
trapped-ion device (IonQ Forte, native arbitrary-angle ZZ, no routing), does measurably more
kernel contrast survive? We do not assume that connectivity alone caused the IBM degradation.
`docs/IONQ_HARDWARE_PLAN.md` sets out the preregistered decision rule and the confounders.

## Status at a glance

* **IBM hardware data exists, but its raw files are withheld.** We ran one job on `ibm_marrakesh`
  (9 qubits, 5 circuits × 4,096 shots = 20,480 shots, 660 CZ per submitted circuit). Its raw job
  files and provider metadata are not in this repository until redistribution rights are
  confirmed (`WITHHELD_EVIDENCE.md`). Nothing synthetic replaces them.
* **Public verification is therefore partial.**
  - Anyone can recompute the circuits, the exact classical reference, the compilation counts and
    the IonQ-native equivalence.
  - The hardware numbers below are reported results. Three verifier checks report `WITHHELD`
    until the raw files are supplied.
* **No IonQ hardware result exists yet.** The 144-ZZ (original structure) and 108-ZZ (simplified)
  circuits have only been compiled and verified locally. Neither has run on IonQ hardware.
* **IonQ has not yet confirmed the native-ZZ parameters.** IonQ's documentation states no ZZ
  angle range, sign convention or precision, and we have not received confirmation from IonQ or
  Qollab.

## What is in this repository

| path | contents | status |
|---|---|---|
| `data/kernel_pair_manifest.json` | the 5 kernel entries (9 distinct TrackML-derived 9-feature vectors), frozen before the IBM run | historical input, CC-BY-4.0 |
| `qkrl/feature_map.py` | exact numpy statevector reference for the kernel | new code, adapted from our earlier code |
| `qkrl/circuits.py` | Qiskit constructions: historical 144-RYY circuit, exact fused 108-RYY form, IonQ-native ZZ forms | new code (historical construction reproduced byte-for-byte) |
| `qkrl/ionq_compile.py` | offline lowering to IonQ Forte native gates {GPI, GPI2, ZZ} via qiskit-ionq | new code |
| `qkrl/metrics.py` | kernel-contrast metrics as defined for the IBM run | new code |
| `circuits/historical/` | sealed QASM3 bytes: logical circuits and the **exact compiled circuits that ran on IBM** | historical artefacts |
| `circuits/historical/offline_optimized/` | exact lower-cost IBM-style compilations found offline. **None of these ever ran on hardware** | historical, offline only |
| `WITHHELD_EVIDENCE.md` / `.json` | list and SHA256 of the IBM job files and provider metadata that are **withheld**. Nothing replaces them | historical physical measurement (withheld) |
| `evidence/historical_derived/` | historical exact-reference and offline-analysis records, kept for comparison only | historical |
| `reference/` | exact classical reference values (and a new 9×9 Gram matrix) | new computation |
| `experiments/` | proposed IonQ circuit set, native payloads (**never submitted**), cost table, power analysis | proposal |
| `verifier/verify.py` | independent end-to-end check (11 checks; 3 report `WITHHELD` in the public release) | new |
| `verifier/audit_ionq_native.py` | IonQ gate-convention and payload-semantics audit against IonQ's documented matrices | new |
| `tests/` | pytest suite | new |

## Reproduce

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python verifier/verify.py           # 8 PASS + 3 WITHHELD (C06-C08 need withheld IBM files), no network
python verifier/audit_ionq_native.py  # IonQ native-gate audit, a few minutes
python -m pytest -q                 # 35 pass, 2 skipped (need withheld IBM files)
python reference/build_reference.py
python experiments/build_ionq_plan.py
python experiments/power_analysis.py
```

The verifier and tests disable sockets for the whole run. No step here contacts a quantum provider.
Byte-identical reproduction of the historical QASM3 files requires `qiskit==2.5.2`. With other
versions the byte-identity checks report `VERSION_DEPENDENT` instead of passing.

## Labelling of every number

| label | meaning |
|---|---|
| **EXACT** | noise-free classical calculation (`qkrl/feature_map.py`) |
| **SIMULATED** | noiseless statevector simulation of a specific circuit artefact |
| **HARDWARE (reported)** | measured on `ibm_marrakesh`, job `da591ju1vhnc73flpn0g`. This is the only hardware run. Raw files are withheld; values are reported, not recomputable here |
| **COMPILED-ONLY** | circuit produced and checked offline, never executed on hardware |
| **PROPOSED** | planned IonQ experiment. No IonQ data exists |

Gate counts are not interchangeable between models. 660 is executed CZ (IBM, with routing).
382, 300 and 135 are offline CZ counts on specific graphs. 108 and 144 are IonQ-native ZZ counts.
`docs/PRIOR_WORK.md` lists each one with its model.

## Further documents

`docs/PRIOR_WORK.md` · `docs/PROVENANCE.md` · `docs/REPRODUCIBILITY.md` ·
`docs/IONQ_HARDWARE_PLAN.md` · `docs/HACKATHON_NON_OVERLAP.md` · `docs/LIMITATIONS.md` ·
`docs/AI_ASSISTANCE.md` · `NOTICE.md` · `WITHHELD_EVIDENCE.md` · `PUBLICATION_RIGHTS_MATRIX.md`

## License

Code and documentation: MIT, Copyright (c) 2026 Murat Keskin (`LICENSE`). The scope is limited to
the files marked MIT in `PUBLICATION_RIGHTS_MATRIX.md`. TrackML-derived data: CC BY 4.0, with
attribution in `NOTICE.md`. The MIT licence does not override third-party licences.

## Acknowledgement

We acknowledge the use of IBM Quantum services for this work. The views expressed are those of
the authors, and do not reflect the official policy or position of IBM or the IBM Quantum team.

## What this repository does not claim

No quantum advantage. No improved classification accuracy. No result for hardware other than one
IBM job of 20,480 shots. The kernel-classifier study this workload comes from (our reproduction of
Duckett et al., PRD 109, 052002) found the quantum kernel did **not** beat a tuned classical RBF SVM
(`docs/PRIOR_WORK.md`).
