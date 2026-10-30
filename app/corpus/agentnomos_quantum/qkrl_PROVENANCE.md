# Provenance

## Sources

Every file under `data/`, `circuits/historical/` and `evidence/` (released or withheld) was copied from a sealed local
checkpoint by a read-only extraction script. Sealed checkpoints were never modified. All 34 ER/QN
checkpoints re-verified against their SHA256 manifests on 2026-10-14, with 0 mismatches.

**SHA256 here proves integrity, not provenance.** A digest shows a file is unchanged since sealing.
It does not prove that the counts came from IBM hardware. The checks that tie the counts to the
hardware run are: the provider-issued job ID, the provider timestamps and usage record inside
`IBM_02_terminal.json`, and the PUB → artefact digest binding (verifier C07). These files are
**withheld** from the public release (`WITHHELD_EVIDENCE.md`). A reader who wants independent
confirmation would need IBM's own job record, which only the account holder can retrieve.

Redacted files are not byte-identical to their sealed originals, so the sealed digest is listed
for the project owner's attestation. The public cannot recompute it.

| public file | release status | sealed source (checkpoint · file) | sealed sha256 | relation |
|---|---|---|---|---|
| `circuits/historical/ibm_marrakesh_compiled_P0.qasm3` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `circuits/ibm_compiled_P0.qasm3` | `ff0ff8e222065248…` | byte-identical |
| `circuits/historical/ibm_marrakesh_compiled_P1.qasm3` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `circuits/ibm_compiled_P1.qasm3` | `f7625cc66f07c209…` | byte-identical |
| `circuits/historical/ibm_marrakesh_compiled_P2.qasm3` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `circuits/ibm_compiled_P2.qasm3` | `e3efcf4222dc28a0…` | byte-identical |
| `circuits/historical/ibm_marrakesh_compiled_P3.qasm3` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `circuits/ibm_compiled_P3.qasm3` | `a37806acfea1247c…` | byte-identical |
| `circuits/historical/ibm_marrakesh_compiled_P4.qasm3` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `circuits/ibm_compiled_P4.qasm3` | `884ebaa5b4063f32…` | byte-identical |
| `circuits/historical/logical_P0.qasm3` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `circuits/logical_P0.qasm3` | `2820000075b934c7…` | byte-identical |
| `circuits/historical/logical_P1.qasm3` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `circuits/logical_P1.qasm3` | `e6902aeab9891bc2…` | byte-identical |
| `circuits/historical/logical_P2.qasm3` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `circuits/logical_P2.qasm3` | `91c16ec5846743cb…` | byte-identical |
| `circuits/historical/logical_P3.qasm3` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `circuits/logical_P3.qasm3` | `cdc6781671d72a23…` | byte-identical |
| `circuits/historical/logical_P4.qasm3` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `circuits/logical_P4.qasm3` | `27e514e44b71b593…` | byte-identical |
| `circuits/historical/offline_optimized/heavyhex_exact_algebraic_P1.qasm3` | **RELEASED** | `cp_nomos_research_er01b2_semantic_compilation_execution_r1_20260823T082126Z` · `candidates/best_P_P1.qasm3` | `d2f43f821bbe0c56…` | byte-identical |
| `circuits/historical/offline_optimized/heavyhex_exact_algebraic_P2.qasm3` | **RELEASED** | `cp_nomos_research_er01b2_semantic_compilation_execution_r1_20260823T082126Z` · `candidates/best_P_P2.qasm3` | `977ba86c5bf94b8b…` | byte-identical |
| `circuits/historical/offline_optimized/heavyhex_exact_algebraic_P3.qasm3` | **RELEASED** | `cp_nomos_research_er01b2_semantic_compilation_execution_r1_20260823T082126Z` · `candidates/best_P_P3.qasm3` | `55a6f268d4aa6f7b…` | byte-identical |
| `circuits/historical/offline_optimized/heavyhex_exact_algebraic_P4.qasm3` | **RELEASED** | `cp_nomos_research_er01b2_semantic_compilation_execution_r1_20260823T082126Z` · `candidates/best_P_P4.qasm3` | `c69593c85af1f8f8…` | byte-identical |
| `circuits/historical/offline_optimized/heavyhex_paritynet_P1.qasm3` | **RELEASED** | `cp_nomos_research_er01b21_topology_intrinsic_execution_r1_20260823T094948Z` · `circuits/best_T6_heavyhex_sealed_P1.qasm3` | `61128756af97bd75…` | byte-identical |
| `circuits/historical/offline_optimized/heavyhex_paritynet_P2.qasm3` | **RELEASED** | `cp_nomos_research_er01b21_topology_intrinsic_execution_r1_20260823T094948Z` · `circuits/best_T6_heavyhex_sealed_P2.qasm3` | `0174dfb205209f3b…` | byte-identical |
| `circuits/historical/offline_optimized/heavyhex_paritynet_P3.qasm3` | **RELEASED** | `cp_nomos_research_er01b21_topology_intrinsic_execution_r1_20260823T094948Z` · `circuits/best_T6_heavyhex_sealed_P3.qasm3` | `64f4eac5091bb1e0…` | byte-identical |
| `circuits/historical/offline_optimized/heavyhex_paritynet_P4.qasm3` | **RELEASED** | `cp_nomos_research_er01b21_topology_intrinsic_execution_r1_20260823T094948Z` · `circuits/best_T6_heavyhex_sealed_P4.qasm3` | `4c27fe5800d89c58…` | byte-identical |
| `circuits/historical/offline_optimized/heavyhex_stock_transpiler_best_P1.qasm3` | **RELEASED** | `cp_nomos_research_er01b2_semantic_compilation_execution_r1_20260823T082126Z` · `candidates/best_G_P1.qasm3` | `d58cbe19ab6d9d07…` | byte-identical |
| `circuits/historical/offline_optimized/heavyhex_stock_transpiler_best_P2.qasm3` | **RELEASED** | `cp_nomos_research_er01b2_semantic_compilation_execution_r1_20260823T082126Z` · `candidates/best_G_P2.qasm3` | `46db62f0ae4bcefa…` | byte-identical |
| `circuits/historical/offline_optimized/heavyhex_stock_transpiler_best_P3.qasm3` | **RELEASED** | `cp_nomos_research_er01b2_semantic_compilation_execution_r1_20260823T082126Z` · `candidates/best_G_P3.qasm3` | `7c358447ca6a02f6…` | byte-identical |
| `circuits/historical/offline_optimized/heavyhex_stock_transpiler_best_P4.qasm3` | **RELEASED** | `cp_nomos_research_er01b2_semantic_compilation_execution_r1_20260823T082126Z` · `candidates/best_G_P4.qasm3` | `bde84977fb1897df…` | byte-identical |
| `circuits/historical/offline_optimized/k9_paritynet_P1.qasm3` | **RELEASED** | `cp_nomos_research_er01b21_topology_intrinsic_execution_r1_20260823T094948Z` · `circuits/best_T0_K9_P1.qasm3` | `84bbbc131f2e5bed…` | byte-identical |
| `circuits/historical/offline_optimized/k9_paritynet_P2.qasm3` | **RELEASED** | `cp_nomos_research_er01b21_topology_intrinsic_execution_r1_20260823T094948Z` · `circuits/best_T0_K9_P2.qasm3` | `d49b356aef57382e…` | byte-identical |
| `circuits/historical/offline_optimized/k9_paritynet_P3.qasm3` | **RELEASED** | `cp_nomos_research_er01b21_topology_intrinsic_execution_r1_20260823T094948Z` · `circuits/best_T0_K9_P3.qasm3` | `79c20aa1f4db16a6…` | byte-identical |
| `circuits/historical/offline_optimized/k9_paritynet_P4.qasm3` | **RELEASED** | `cp_nomos_research_er01b21_topology_intrinsic_execution_r1_20260823T094948Z` · `circuits/best_T0_K9_P4.qasm3` | `f0f04789826f0bab…` | byte-identical |
| `data/kernel_pair_manifest.json` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `08_KERNEL_PAIR_MANIFEST.json` | `54230a78cad7d3f7…` | redacted / subset |
| `evidence/historical_derived/er01b21_reconstruct_216.json` | **RELEASED** | `cp_nomos_research_er01b21_topology_intrinsic_execution_r1_20260823T094948Z` · `derived/reconstruct_216.json` | `de78d9268cf76402…` | redacted / subset |
| `evidence/historical_derived/er01b21_topology_scan.json` | **WITHHELD** | `cp_nomos_research_er01b21_topology_intrinsic_execution_r1_20260823T094948Z` · `derived/topology_scan.json` | `00e5576d7307c838…` | redacted / subset |
| `evidence/historical_derived/er01b22_native_mapping.json` | **RELEASED** | `cp_nomos_research_er01b22_real_backend_discovery_execution_r1_20260823T114225Z` · `derived/native_mapping.json` | `43474fc1809ed080…` | redacted / subset |
| `evidence/historical_derived/er01b2_final_analysis.json` | **WITHHELD** | `cp_nomos_research_er01b2_semantic_compilation_execution_r1_20260823T082126Z` · `derived/final_analysis.json` | `a87f8df57ad387b8…` | redacted / subset |
| `evidence/historical_derived/er01b_analysis.json` | **WITHHELD** | `cp_nomos_research_er01b_physical_kernel_pilot_execution_r1_20260823T062357Z` · `derived/analysis.json` | `b88927927543eba8…` | redacted / subset |
| `evidence/historical_derived/er01b_stats_addendum.json` | **WITHHELD** | `cp_nomos_research_er01b_physical_kernel_pilot_execution_r1_20260823T062357Z` · `derived/stats_addendum.json` | `6e9eba5d56de700d…` | redacted / subset |
| `evidence/historical_derived/finite_shot_reference.json` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `derived/finite_shot_reference.json` | `1a183d0c6c816844…` | redacted / subset |
| `evidence/historical_derived/ibm_compilation.json` | **WITHHELD** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `derived/ibm_compilation.json` | `cd4de98d415267a9…` | redacted / subset |
| `evidence/historical_derived/local_reference.json` | **RELEASED** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `derived/local_reference.json` | `45787e6ae55c7453…` | redacted / subset |
| `evidence/ibm_marrakesh_job_da591ju1vhnc73flpn0g/ER01B-R1__IBM_PILOT_BATCH.attempt.json` | **WITHHELD** | `cp_nomos_research_er01b_physical_kernel_pilot_execution_r1_20260823T062357Z` · `raw/ER01B-R1__IBM_PILOT_BATCH.attempt.json` | `47312d052c18db38…` | redacted / subset |
| `evidence/ibm_marrakesh_job_da591ju1vhnc73flpn0g/IBM_00_presubmission.json` | **WITHHELD** | `cp_nomos_research_er01b_physical_kernel_pilot_execution_r1_20260823T062357Z` · `raw/IBM_00_presubmission.json` | `92a823a873b7f2e5…` | redacted / subset |
| `evidence/ibm_marrakesh_job_da591ju1vhnc73flpn0g/IBM_01_submission.json` | **WITHHELD** | `cp_nomos_research_er01b_physical_kernel_pilot_execution_r1_20260823T062357Z` · `raw/IBM_01_submission.json` | `b536d69edccff4cc…` | redacted / subset |
| `evidence/ibm_marrakesh_job_da591ju1vhnc73flpn0g/IBM_02_terminal.json` | **WITHHELD** | `cp_nomos_research_er01b_physical_kernel_pilot_execution_r1_20260823T062357Z` · `raw/IBM_02_terminal.json` | `f8ff6a4b11ee4e52…` | redacted / subset |
| `evidence/ibm_marrakesh_target_subset.json` | **WITHHELD** | `cp_nomos_research_er01b_physical_kernel_pilot_release_r1_20260823T060706Z` · `artifacts/ibm_target_snapshot.json` | `cecee09b1ea63b3b…` | redacted / subset |

## Redaction policy

Removed: IBM Cloud instance CRN (`instance_id`), `plan_id`, account-ID substrings, and local
filesystem paths (replaced by `<private-path>/<basename>`). Measurement counts, job ID, backend
name, timestamps, shot counts and calibration values are unchanged. The historical execution
checkpoint left the CRN unredacted in two raw files. That was recorded at the time as erratum
ER01B-E1: a service identifier, not a credential. This package does not republish it.

## Rights

| item | status |
|---|---|
| Code and documentation | Murat Keskin (AgentNOMOS Research Lab), produced with AI assistance (`docs/AI_ASSISTANCE.md`). MIT, scoped by `PUBLICATION_RIGHTS_MATRIX.md` |
| TrackML-derived vectors | CC-BY-4.0 (Zenodo record checked 2026-10-14). Attribution and change notice in `NOTICE.md` |
| IBM raw job output and provider metadata | **WITHHELD.** IBM's public FAQ asks publications to acknowledge IBM Quantum, but it says nothing on ownership or redistribution of job data, and the binding service terms could not be retrieved publicly. Not covered by MIT |
| Summary hardware results | Reported as the authors' findings, with IBM's recommended acknowledgement (README) |
| Duckett et al. paper | Cited only. PDF and LaTeX source excluded |
| Raw TrackML event files (~5 GB) | Excluded. Available from Zenodo |
