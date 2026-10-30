# Limitations and negative results

* **One hardware run.** The IBM baseline is a single job: 5 circuits, 4096 shots each, no
  replicates. Calibration drift between days is not characterised for this workload.
* **Five kernel entries, not a kernel matrix.** The hardware measured 5 entries. The 9×9 Gram
  matrix in `reference/` is exact classical computation only.
* **The depolarising model is a summary, not a mechanism.** It fits the IBM data (χ² = 5.65 on
  4 dof). No noise mechanism is identified.
* **No classifier result on hardware.** The ER-01B classifier probe was one entry's sensitivity.
  It is not a hardware classifier.
* **The underlying quantum kernel was not competitive classically** (ER-01A). This project is
  about whether hardware preserves kernel *contrast*, not whether the kernel is useful.
* **IonQ counts are compiled locally.** IonQ's server-side handling, ZZ angle limits and
  precision, and gate parallelism are unverified until IonQ/Qollab confirm them or a run occurs.
* **Costs are list-price references.** The Qollab credit accounting is unknown.
* **Provenance limits.** SHA256 shows integrity since sealing, not origin. Only the account
  holder can retrieve IBM's own record of the job.
* **AI-assisted work.** The historical research and this package were produced with substantial
  AI assistance (`docs/AI_ASSISTANCE.md`). Every number in this package is recomputed by code in
  the repository rather than taken from narrative reports.
* **Withheld raw evidence.** The public release does not contain the IBM job files. The hardware
  numbers are therefore reported results and cannot be recomputed from this repository. They can
  be verified with `--evidence-dir` by anyone the owner gives the withheld files to. Their digests
  are published in `WITHHELD_EVIDENCE.json`.
