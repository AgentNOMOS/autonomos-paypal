# Reproducibility

## Environment

Python 3.12, with the exact pins in `requirements.txt`. The package was rebuilt from scratch in a
fresh virtual environment from those pins, and the full verifier and test suite ran with the
network namespace disabled (`unshare -n`). Results are in `reports/`.

## What the verifier recomputes (`python verifier/verify.py`)

| check | recomputed from | compared with |
|---|---|---|
| C01 | feature vectors (float64 bytes) | digests frozen before the IBM run |
| C02 | `qkrl.feature_map` (pure numpy) | sealed exact kernel values, tol 1e-12 |
| C03 | Qiskit statevector of the logical circuit | same, tol 1e-12 (independent of C02) |
| C04 | rebuilt logical circuits → QASM3 | sealed QASM3 bytes (qiskit 2.5.2) |
| C05 | sealed compiled IBM circuits | 660 CZ, depth 1668, 9 physical qubits, noiseless P(0) = K_exact |
| C00 | withheld files, if supplied | their SHA256 vs `WITHHELD_EVIDENCE.json` |
| C06 | the frozen IBM transpilation, re-run offline (seed 20260823, opt. level 0, SABRE) | sealed compiled bytes. **WITHHELD** in the public release: needs the IBM coupling map |
| C07 | raw job file | job ID, backend, `DONE`, 5 × 4096 = 20,480 shots, PUB → circuit digest binding, no mitigation. **WITHHELD** |
| C08 | raw counts | all historical contrast metrics (self-overlap, dynamic range, MAE, ρ, f and its CI). **WITHHELD** |
| C09 | offline-optimised IBM-style circuits | recorded counts 496 / 382 (P1: 495) / 300 / 135 and exactness |
| C10 | exact layer fusion | 144 → 108 RYY; P0 → empty; kernel unchanged |
| C11 | IonQ-native lowering (qiskit-ionq gate set) | 144 / 108 ZZ, exactness ≤ 1e-10, folded angles within ±0.25 turns |

`verifier/audit_ionq_native.py` additionally checks the IonQ gate conventions against the
documented matrices, the full-unitary equivalence of all native constructions, and every payload.
See `docs/IONQ_HARDWARE_PLAN.md` §3.1–3.2.

## Running the withheld checks

```bash
python verifier/verify.py --evidence-dir /path/to/withheld/evidence
```

This needs `ibm_marrakesh_job_da591ju1vhnc73flpn0g/`, `ibm_marrakesh_target_subset.json` and
`historical_derived/er01b_stats_addendum.json` laid out as in `WITHHELD_EVIDENCE.json`.

## What cannot be reproduced by a third party

* **The hardware measurement itself.** It was a single run on a device whose calibration has
  since changed. Its raw output is withheld from this release. Anyone re-running it would get a
  new measurement, not a reproduction of this one.
* The redacted fields (IBM account identifiers), deliberately.
* The upstream TrackML preprocessing (ER-01A) is not re-executed here, because that needs a
  ~1.9 GB download. The 9 vectors are published directly with their pre-run digests.

## Determinism

Exact kernels, circuit construction and the IBM transpilation are deterministic (fixed seeds).
The IonQ lowering uses `seed_transpiler=20261014`. The power analysis uses PCG64 seed 20261014.
