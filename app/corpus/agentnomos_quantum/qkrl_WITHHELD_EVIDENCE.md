# Withheld experimental evidence

The files below are **not** in this public release. They exist, sealed, with the project owner.
They are withheld because we could not establish from IBM's public documentation whether raw
job output and provider metadata may be redistributed. They have **not** been replaced by
simulated or synthetic data. The hardware values in `README.md` are reported results; this
repository alone cannot recompute them.

Verifier checks C06, C07 and C08 report `WITHHELD`. A reviewer who receives these files from
the owner can run `python verifier/verify.py --evidence-dir DIR`. Check C00 then confirms each
file against the SHA256 below before the hardware checks run.

| file (relative to evidence dir) | bytes | sha256 (redacted form) | reason withheld |
|---|---:|---|---|
| `ibm_marrakesh_job_da591ju1vhnc73flpn0g/IBM_00_presubmission.json` | 9404 | `8bbc20c6379f8ac0c4c65c4120eec07901db7ada3f1de72bd4a735d76aa4fd26` | provider-returned backend status, calibration values and usage record |
| `ibm_marrakesh_job_da591ju1vhnc73flpn0g/IBM_01_submission.json` | 273 | `b536d69edccff4cc6e51aa7bb8787d3f3611caae0b8475a3323a93b1e1f20ded` | provider job submission record |
| `ibm_marrakesh_job_da591ju1vhnc73flpn0g/IBM_02_terminal.json` | 63168 | `ea2d2d85a478409c9e2606ed9c4bc60ac53330bdf8f8dbb191c04d8e33d72baa` | raw provider output: 512-outcome count histograms, execution spans, job metrics, usage |
| `ibm_marrakesh_job_da591ju1vhnc73flpn0g/ER01B-R1__IBM_PILOT_BATCH.attempt.json` | 295 | `47312d052c18db38afec9809e4e47a55e7f962da8aa624057c264e3457e48555` | attempt lock bound to the provider job record |
| `ibm_marrakesh_target_subset.json` | 8352 | `2cd05c078f3df2eb2d658033b894169c6da7d19331198ef4a79242c582fff7fb` | provider metadata: ibm_marrakesh coupling map and basis gates |
| `historical_derived/er01b_analysis.json` | 4066 | `b88927927543eba887adc203dcffc1e1536bb4274ebbc7852a42893fb544e864` | derived directly from the raw provider counts |
| `historical_derived/er01b_stats_addendum.json` | 1534 | `6e9eba5d56de700dc168a3ce28923540972226b4f4d13de9f177763791768503` | derived directly from the raw provider counts |
| `historical_derived/ibm_compilation.json` | 4977 | `cd4de98d415267a9477522951c2ab02fcb3fb40ea2121d6ab248bbae29969300` | compilation log bound to the provider target snapshot |
| `historical_derived/er01b21_topology_scan.json` | 2742745 | `00e5576d7307c838eb8819bc4916cfcf77bd0c540bfd34565a365f94f194aed6` | embeds provider coupling graphs (IBM heavy-hex, QuTech Tuna-9) |
| `historical_derived/er01b2_final_analysis.json` | 12903 | `a87f8df57ad387b8b2e845760791a90c67fbe1d51a01921c4137f88fbb763e80` | embeds provider physical-qubit layouts |

What remains public: the input vectors (CC-BY-4.0), our logical circuits and the exact compiled
circuits that were submitted (byte-identical to the sealed files), the exact classical reference,
the offline compilation artefacts, the IonQ plan and payloads, the verifier, tests and docs.
