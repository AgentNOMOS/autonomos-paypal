# Prior work: what existed before this repository

All dates are UTC. All the work below was finished by **2026-08-23** and sealed in local SHA256
manifests. That is about seven weeks before the Qollab × IonQ hackathon build window opened
(2026-10-09 23:00 UTC). This package republishes that work. It adds no new hardware data.

## Timeline

| date | phase | what happened | hardware jobs |
|---|---|---|---|
| 2026-08-22 | QN-00 … QN-05 | Internal calibration studies: provider access audit, then 3-qubit mirror-circuit depth-scaling and replication canaries on `ibm_marrakesh` and QuTech Tuna-9 (free tiers). **Not part of this package**; listed only for the job ledger | 27 (IBM + QuTech, free) |
| 2026-08-22/23 | ER-01A | Reproduction of Duckett et al., *PRD* 109, 052002 (2024) on TrackML data with an **exact** statevector kernel. Result: `PARTIAL_REPRODUCTION`. In the preregistered configuration, the published direction (quantum > classical) was **not** reproduced. Under one admissible α convention it was reproduced, but never against a tuned classical RBF SVM | 0 |
| 2026-08-23 06:18 | ER-01B | **One** IBM job: the exact 9-qubit kernel circuit for 5 frozen kernel entries, 4096 shots each | **1** (`ibm_marrakesh`, job `da591ju1vhnc73flpn0g`, 9 s of free-plan QPU time, USD 0.00) |
| 2026-08-23 | ER-01B2 | Offline search for exact compilations on the same heavy-hex target | 0 |
| 2026-08-23 | ER-01B2.1 | Offline analysis of how topology and synthesis affect gate cost | 0 |
| 2026-08-23 | ER-01B2.2 | Read-only discovery of backends and costs; exact native mapping for trapped-ion gate sets | 0 |
| 2026-08-23 | ER-01 closure | Phase closed. Hardware branch `PAUSED_ACCESS_OR_COST_BLOCKER`; "apply for research credits" listed as a future option | 0 |

## The workload

* 9 features → 9 qubits. Feature map from Duckett et al. Eq. (7): Z terms `φ_k = x_k`, pairwise
  `Y_lY_m` terms `φ_lm = (π−x_l)(π−x_m)`, α = 0.1 (literal Eq. 7), 2 repetitions, ORDERED
  product decomposition.
* Kernel entry `k(x,z) = |⟨0|U(z)†U(x)|0⟩|²`, estimated on hardware as the probability of reading
  all-zero.
* Five frozen entries, chosen by rule before any hardware contact: P0 = self-overlap (exact 1),
  P1 = maximum training-pair kernel, P2 = minimum, P3 = closest to 0.5, P4 = a support-vector
  entry for a classifier-sensitivity probe.

## Gate counts: each number belongs to a different model

| count | gate type | model | status |
|---:|---|---|---|
| **660** CZ, depth 1668 | CZ + RZ/SX/X | historical 144-RYY circuit, Qiskit optimisation level 0, SABRE routing on the `ibm_marrakesh` heavy-hex graph, physical qubits 68,69,70,78,89–93 | **HARDWARE**: executed for all of P0–P4 |
| 496 CZ | CZ | best stock-transpiler candidate, same target | COMPILED-ONLY |
| **382** CZ (P2–P4), 495 (P1) | CZ | exact algebraic layer fusion + transpiler, same target. P1 is higher because the transpiler deletes P1's tiny rotations as "identity" at optimisation level ≥ 2, so the circuits that would be exact were rejected | COMPILED-ONLY |
| 300 CZ (P1–P3), 349 (P4) | CZ | parity-network synthesis on the sealed heavy-hex graph | COMPILED-ONLY |
| 216 | CNOT | 108 fused RYY × 2 CNOT each. A decomposition cost, **not** a lower bound | THEORETICAL |
| **135** | CNOT | parity network (3 blocks × 45 CNOT) on an abstract all-to-all graph K9. Best known with **fixed-angle** entanglers; not proven optimal | THEORETICAL |
| **108** | arbitrary-angle ZZ | 3 commuting blocks × 36 pairwise phases, one native gate each, no routing (IonQ Forte ZZ / Quantinuum RZZ). Also a lower bound *within the CNOT+phase model* proved in ER-01B2.1 | COMPILED-ONLY (re-derived here with qiskit-ionq: `verifier` C11) |
| **144** | arbitrary-angle ZZ | the unfused historical circuit mapped layer-for-layer (4 × 36) | COMPILED-ONLY (new in this package) |
| 288 | fixed-angle ZZ(1/4) | stock Qiskit + qiskit-ionq lowering of the historical circuit (2 per RYY) | COMPILED-ONLY (new in this package) |
| 0 | — | P0 (x = z) fused: the overlap is exactly the identity | COMPILED-ONLY |

"Depth 33" is the logical 2Q-layer depth of the 108-ZZ form under lexicographic gate order. It is
not a hardware duration. IonQ does not document whether ZZ gates run in parallel on one ion chain.

## Kernel-contrast definition (unchanged here)

`K_hw = count('000000000') / shots`. Self-overlap = `K_hw(P0)`. Dynamic range =
`K(P1) − K(P2)`. Retained fraction = `DR_hw / DR_exact`. Single-parameter global depolarising fit:
`P_obs = f·K_exact + (1−f)/512`, using a binomial MLE with a 95 % profile-likelihood interval.

## Negative results carried forward unchanged

1. ER-01A: the quantum kernel was **worse** than a tuned classical RBF SVM at every training size
   and under both α conventions (12/12 intervals exclude zero).
2. ER-01B: self-overlap 0.0068 instead of 1. Retained dynamic range 0.318 %. f = 0.0048.
   The exact value lay inside the hardware 95 % CI for **0/5** entries. All 5 entries fell beyond the
   maximum of 10,000 shot-noise-only simulations, so shot noise alone does not explain them. No
   microscopic noise mechanism was identified or claimed.
3. ER-01B: rank order was partly preserved (ρ = 0.8). Only 2 of 7 declared pairwise separations
   were individually significant. **Rank correlation alone must not be used to certify kernel
   hardware.**
4. ER-01B: a classifier-margin probe kept its sign by luck, not robustness. The flip threshold
   was |ΔK| = 8.5e-8 and the observed |ΔK| was 0.092.
5. ER-01B: the QuTech Tuna-9 arm was blocked (qubit Q8 under a provider warning, no spare qubit).
   Nothing was run there and no approximate substitute was built.
6. ER-01B2: 140 candidate circuits with *fewer* gates changed the kernel and were rejected. Fewer
   gates does not mean the same semantics.
7. ER-01B2.1: Qiskit 2.5.2 `synth_cnot_phase_aam` applies `p(angle % π)`, which corrupts angles
   outside [0, π). We worked around it and documented it.
8. Process defects were self-reported at the time, for example a secret-scan summary line that
   contradicted its own count (erratum ER01B-E1). See `docs/PROVENANCE.md`.
