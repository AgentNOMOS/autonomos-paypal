# IonQ hardware experiment plan (PROPOSED — no IonQ job has been run)

## 1. Research question

When the same 9-qubit quantum-kernel workload runs on a trapped-ion device with all-to-all
connectivity and a native arbitrary-angle entangler (IonQ Forte), does measurably more kernel
contrast survive than in the 2026-08-23 IBM baseline (`ibm_marrakesh`, f = 0.0048,
retained dynamic range 0.318 %)?

We do **not** assume that connectivity caused the IBM degradation. The IBM circuit carried
several cost factors at once:

| factor | IBM run | IonQ plan | separable here? |
|---|---|---|---|
| routing/SWAP overhead | 660 − 2×144 = 372 CZ beyond the 2-CZ-per-RYY decomposition | 0 (all-to-all) | partly: arms A vs E |
| entangler type | fixed-angle CZ, 2 per RYY | arbitrary-angle ZZ, 1 per RYY (arm A); fixed ZZ(1/4), 2 per RYY (arm E) | **yes: arm A vs arm E** |
| exact algebraic simplification | none (P0 executed at full depth) | none in arm A; fused in arm B | yes: arm A vs arm B |
| 2Q gate error, 1Q error, readout | IBM calibration of 2026-08-22 (provider values withheld with the raw evidence) | IonQ calibration on the run day | **no**, different technology. Reported, not controlled |
| coherence vs duration | superconducting T1/T2, 1668 layers | trapped-ion coherence, gates likely serial | no. Reported, not controlled |
| measurement error | no mitigation | no mitigation (debiasing **off**); SPAM measured by control C | partly |
| time / hardware generation | Aug 2026 | run date | **no**. An unavoidable confounder |

## 2. Workload (reconstructed and verified)

* 9 qubits; the same 5 kernel entries and the same 9 distinct input vectors as the IBM run
  (`data/kernel_pair_manifest.json`).
* Logical equivalence to the historical reference: every circuit below has noiseless
  P(all-zero) equal to the sealed exact kernel to ≤ 1e-10 (`verifier/verify.py` C11, and the
  plan builder asserts it for each circuit).

## 3. Compilation for the IonQ Forte native gate set (done locally, real counts)

Tooling: `qiskit==2.5.2`, `qiskit-ionq==1.1.1` (IonQ's own Qiskit provider) gate classes and
equivalence library, target {GPI, GPI2, ZZ} = the Forte family native set in qiskit-ionq. IonQ's
documentation lists ZZ as Forte's native 2Q gate. Optimisation level 3. **No IonQ API call.**

The **primary payloads** use the exact quarter-turn fold (§3.2), so every ZZ angle is within ±0.25
turns. The unfolded alternatives are in `experiments/ionq_native_payloads/unfolded/`.

| circuit set | what it is | native ZZ | GPI+GPI2 (folded) | ZZ depth (logical) |
|---|---|---:|---:|---:|
| **A_full_depth** (P0–P4) | historical circuit mapped layer-for-layer: 4 YY layers × 36 → one arbitrary-angle ZZ per RYY. Same logical workload as IBM, **including a full-depth P0** | **144** | 528–864 | 42 |
| **B_fused** (P1–P4) | exact fused form (ER-01B2 algebra). Best-known exact native cost | **108** | 504–648 | 33 |
| **E_stock_fixed_angle** (P0–P4) | what stock `transpile()` produces from the historical circuit: each RYY → 2 × ZZ(1/4) | **288** | 5832 | 96 |
| C_spam_P0_fused | P0 fused to the empty circuit: state preparation + readout only | 0 | 0 | 0 |
| D_half_depth_P0 | control: reps = 1 self-mirror of P0, unfused | 72 | 210 | 24 |
| A_full_depth_reordered (P0–P4) | optional, unbudgeted: arm A with the ZZ order permuted inside each commuting block (exact). Probes order/crosstalk sensitivity | 144 | 528–864 | 59–85 |

Stock tooling does **not** find the 108/144 path: it lowers each RYY to two fixed-angle ZZ(1/4),
giving 288 ZZ. The 1-ZZ-per-RYY mapping is the exact basis change in `qkrl/circuits.py`
(YY = V·ZZ·V†, V = S·H).

### 3.1 Gate-parameter convention (audited, `verifier/audit_ionq_native.py`)

| quantity | IonQ documentation (read 2026-10-14) | qiskit-ionq 1.1.1 | used here |
|---|---|---|---|
| ZZ | `ZZ(θ) = exp(−iπθ Z⊗Z) = diag(e^{−iπθ}, e^{iπθ}, e^{iπθ}, e^{−iπθ})`; parameters "in turns, 1 turn = 2π radians"; API field `angle`, with `targets` | `ZZGate(θ)`, same matrix (max deviation 0); QASM definition `rzz(2πθ)` | `angle = −θ_lm/π` for the target `exp(iθ_lm Z_lZ_m)` |
| GPI / GPI2 | matrices with `e^{±2πiφ}`, φ in turns; API field `phase`; accepted range **−1 … 1** | same matrices (deviation 0) | all phases within [−1, 1] (guarded) |

"Turns" applies to the rotation angle: ZZ(θ) equals Qiskit's `RZZ(2πθ)` in radians. Equivalently,
θ is the coefficient of the exponent in units of π. Both readings give the same matrix. The audit
shows that two misreadings would be caught: `exp(−i2πθ ZZ)` (deviation 1.37) and `exp(−iθ ZZ)` in
radians (deviation 0.98).

### 3.2 Equivalence and angle normalisation

* For all 5 entries × 4 constructions (A, B, each folded and unfolded), the full 512×512 unitary
  equals the historical logical circuit's unitary up to global phase (max deviation 3.2e-14),
  both before and after lowering to {GPI, GPI2, ZZ}.
* Lowering at optimisation level 3 leaves the 2Q layer untouched: the same number of ZZ and the
  identical multiset of (qubit pair, angle).
* Fold rule: `ZZ(θ) = ZZ(θ − k/2)·(Z⊗Z)^k · (−i)^k`. For odd k, it adds one Z on each of the two
  qubits, which the 1Q lowering absorbs into GPI/GPI2 phases. **The ZZ count is unchanged (144 /
  108); only the 1Q count changes.** Maximum deviation of the identity: 1.7e-15.
* Every primary and alternative payload JSON was re-simulated with only the documented matrices
  (pure numpy, no qiskit-ionq). Each one reproduces the reference unitary up to global phase
  (max deviation 3.6e-13).

### 3.3 Unresolved hardware restrictions (not assumed; to confirm with IonQ/Qollab)

1. **ZZ angle range.** It is not documented: MS is documented as 0–0.25 turns, ZZ has no stated
   range. The folded payloads stay within ±0.25.
2. **Negative ZZ angles** are neither documented as accepted nor as rejected. An exact rewrite to
   non-negative angles exists but is not applied.
3. **ZZ angle precision.** It is not documented. For MS, IonQ states about 3 decimal places. A
   1e-3-turn quantisation would be a systematic approximation, and its effect on K must be
   bounded before any run.
4. Gate parallelism, physical pulse implementation and per-circuit gate limits are not documented.
5. **Access path.** IonQ's own API lists `zz` for Forte. The AWS Braket developer guide lists only
   gpi/gpi2/ms for IonQ verbatim circuits, so native ZZ through Braket is **unconfirmed**.

Logical vs physical: one `zz` instruction is one native entangling operation as IonQ defines it.
"ZZ depth" is a logical figure; the edge-colouring minimum for 3 blocks on K9 is 27.

Submission mode: native gates only, so that per IonQ's documentation the compiler and optimiser
are bypassed. Debiasing/error mitigation stays off. Payloads are in
`experiments/ionq_native_payloads/` and were **never submitted**.

## 4. Kernel matrix and controls

* Distinct data vectors: **9**. Kernel entries: **5** (P0 self-overlap, P1 max, P2 min, P3 ≈ 0.5,
  P4 classifier-boundary entry), identical to IBM. No new entries, so the comparison stays
  apples-to-apples.
* Qubits: 9 of 36 (IonQ assigns the ions).
* Controls: C (SPAM/readout floor), D (depth decay of the self-overlap), A vs B (does exact
  simplification help), A vs E (arbitrary- vs fixed-angle entangler without routing). The
  numerically exact `reference/exact_kernel_reference.json` supplies the ground truth.

## 5. Budgets (generated by `experiments/build_ionq_plan.py`)

| budget | circuit sets | circuits | shots / circuit | replicates | tasks | total shots | max 2Q / circuit | AWS Braket list price | Azure Quantum list price (EM off, folded 1Q counts) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| MINIMUM PILOT | A (P0–P4) + C | 6 | 1,000 | 1 | 6 | **6,000** | 144 | **USD 481.80** | USD 1,363.92 |
| **RECOMMENDED REPRODUCTION — primary request** | A + B + C + D | 11 | 1,000 | 2 (separate calibration days) | 22 | **22,000** | 144 | **USD 1,766.60** | USD 4,655.30 |
| EXTENDED ROBUSTNESS | A + B + E + C + D | 16 | 2,000 | 3 | 48 | **96,000** | 288 | **USD 7,694.40** | USD 52,354.88 |

How the Braket figure is calculated, at USD 0.30 per task + USD 0.08 per shot:

| budget | tasks | per-task cost | shots | per-shot cost | total |
|---|---|---:|---|---:|---:|
| pilot | 6 | 1.80 | 6,000 | 480.00 | **481.80** |
| recommended | 22 | 6.60 | 22,000 | 1,760.00 | **1,766.60** |
| extended | 48 | 14.40 | 96,000 | 7,680.00 | **7,694.40** |

Pricing sources (retrieved 2026-10-14):
* AWS Braket, IonQ Forte: USD 0.30 per task + USD 0.08 per shot
  (<https://aws.amazon.com/braket/pricing/>). Braket requires ≥ 2,500 shots/task only when error
  mitigation is on; it stays off here. Braket lists "Forte" only. Whether Braket accepts native ZZ
  is unconfirmed (§3.3).
* Azure Quantum, IonQ Forte 1: USD 0.0001645 per 1Q gate-shot + USD 0.001121 per 2Q gate-shot,
  minimum USD 25.7899 per program with error mitigation off
  (<https://learn.microsoft.com/en-us/azure/quantum/pricing>, page updated 2026-04-23).
* **Qollab research-credit accounting: UNKNOWN.** The USD figures are list-price references only.

### Questions for Qollab before sizing the award

1. Credit valuation and the hardware provider/access path the credits apply to (IonQ cloud
   direct, AWS Braket, Azure Quantum, other).
2. Which IonQ Forte-class (or equivalent) backend is available to us.
3. Native ZZ support on that path, plus its angle range, sign convention and precision.
4. The actual chargeable workload units (per task, per shot, per gate-shot; minimums;
   charges for failed or cancelled tasks).

## 6. Preregistered decision rule

Primary statistic: the surviving contrast f in `P_obs = f·K_exact + (1−f)/512`. It is fitted by
binomial maximum likelihood to arm A (P0–P4) with a 95 % profile-likelihood interval. This is the
same statistic, model and code (`qkrl/metrics.py`) as the IBM baseline. Threshold **f* = 0.05**:
5 % of ideal contrast, about 10× the IBM point estimate.

| outcome | criterion |
|---|---|
| **IMPROVED CONTRAST** | lower 95 % bound of f ≥ 0.05, **and** the controls are valid |
| **NO MEANINGFUL IMPROVEMENT** | upper 95 % bound of f < 0.05 (a statistically detectable but sub-threshold change from 0.0048 is reported descriptively and not called an improvement) |
| **INCONCLUSIVE** | the interval contains 0.05; **or** control C has P(0⁹) < 0.80; **or** the depolarising model is rejected (χ² p < 0.001), in which case the model-free secondaries below decide whether the result is reported as improvement; **or** the provider recompiled the circuits or returned partial results |

Secondary statistics, model-free and reported in every case: K_hw(P0) with its Wilson CI;
retained dynamic range (K_hw(P1) − K_hw(P2)) / 0.998951; Spearman ρ on P1–P4 (never used alone);
exact value inside the CI (count out of 5); the per-entry error table in the same format as the
IBM baseline.

Power (`experiments/power_analysis.py`, 400 Monte-Carlo trials per cell, global-depolarising
model): at 1,000 shots/circuit, P(IMPROVED) = 0.98 if the true f = 0.07 and 1.00 if f ≥ 0.10;
P(NO MEANINGFUL IMPROVEMENT) = 1.00 if f ≤ 0.03. True values near 0.05 are mostly
inconclusive by design.

Illustration only, **not** a prediction and **not** an IonQ specification: if each ZZ had error ε
and errors combined multiplicatively, arm A would give f ≈ (1−ε)^144: ε = 0.5 % → 0.49,
ε = 1 % → 0.24, ε = 2 % → 0.055. The experiment exists because this cannot be assumed.

## 7. Comparison protocol

1. Identical inputs, entries, kernel definition, estimator, statistics and code to the IBM baseline.
2. Arm A is the primary comparison: same logical circuit, compiled natively. Arm B is reported
   separately as "best exact compilation". It is never mixed into the arm-A statistic.
3. No error mitigation, readout correction or post-selection on either platform.
4. The circuits are frozen and hashed before submission. One submission attempt per task. Failures
   are reported, not retried silently.
5. Disclosed confounders that cannot be removed: different hardware technology and generation;
   different dates and calibrations; IBM was run once (no replicates); compilation differs (that
   is the variable under test, partly separated by arms A/B/E); IBM's 660-CZ circuit included
   routing, so "IBM with all-to-all" cannot be observed.
6. Out of scope: quantum advantage, classification accuracy, vendor ranking. The study measures
   the survival of kernel contrast only.
