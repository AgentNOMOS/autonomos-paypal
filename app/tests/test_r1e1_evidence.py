"""R1e.1 Part A: false document support for non-numeric conclusions. Written BEFORE the R1e.1 rules
(record: r1e1/evidence/test_first_part_a.txt). Every wrong claim quotes a real passage verbatim and is tried under
all seven evidence classes (the model picks the class); it must never come out CLAIM_SUPPORTED. Controls must stay
supported. A separate held-out set, written after the rules, lives in evidence/scripts/nonnumeric_probe.py.
Run from app/: python3 -B -m unittest tests/test_r1e1_evidence.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_r1e_unified import CLASSES, outcome  # noqa: E402

R2, R6, R8, R15 = "qkrl_README.md#p002", "qkrl_README.md#p006", "qkrl_README.md#p008", "qkrl_README.md#p015"
L1, PW = "qkrl_LIMITATIONS.md#p001", "qkrl_PRIOR_WORK.md#p003"
RAN_ONCE = "This package reproduces a 9-qubit quantum-kernel experiment that we ran once on IBM Quantum hardware"
RECOMP = ("Every number in this package is recomputed by code in the repository rather than taken from narrative "
          "reports.")
SINGLE = "The IBM baseline is a single job: 5 circuits, 4096 shots each"
REPORTED = "The hardware numbers below are reported results."
FITS = "It fits the IBM data (χ² = 5.65 on 4 dof)."
DRIFT = "Calibration drift between days is not characterised for this workload."
ONE_JOB = "We ran one job on `ibm_marrakesh`"
HW_ROW = ("measured on `ibm_marrakesh`, job `da591ju1vhnc73flpn0g`. This is the only hardware run. Raw files are "
          "withheld; values are reported, not recomputable here")


def best(anchor, quote, claim):
    """(supported_classes, decision under UNKNOWN_OR_UNSUPPORTED, reasons)."""
    sup = [c for c in CLASSES if outcome(anchor, quote, claim, c)[0] == "SUPPORTED"]
    d, r = outcome(anchor, quote, claim, "UNKNOWN_OR_UNSUPPORTED")
    return sup, d, " ".join(r.get("reasons", []) + r.get("claim_unconfirmed_reasons", []))


class K01KnownR1eFailures(unittest.TestCase):
    def test_once_becomes_twice(self):
        sup, d, why = best(R2, RAN_ONCE, "The documents report that this package reproduces a 9-qubit quantum-kernel "
                                         "experiment that they ran twice on IBM Quantum hardware.")
        self.assertEqual(sup, [])
        self.assertIn("COUNT", why)

    def test_reported_versus_recomputed_reversed(self):
        sup, d, why = best(L1, RECOMP, "The documents report that every number in this package is taken from "
                                       "narrative reports rather than recomputed by code in the repository.")
        self.assertEqual(sup, [])
        self.assertIn("CONTRAST_REVERSED", why)


class K01bHardWhereTheEvidenceContradicts(unittest.TestCase):
    """A count that conflicts with the evidence, and a claim that asserts only the rejected side of a contrast, are
    WITHHELD (not merely unconfirmed)."""

    def test_count_conflict_is_withheld(self):
        d, r = outcome(R2, RAN_ONCE, "The documents report that this package reproduces a 9-qubit quantum-kernel "
                                     "experiment that they ran twice on IBM Quantum hardware.", "HARDWARE_EXECUTION")
        self.assertEqual(d, "WITHHELD")
        self.assertTrue(any(x.startswith("COUNT_CHANGED") for x in r["reasons"]), r["reasons"])

    def test_asserting_the_rejected_side_is_withheld(self):
        for cls in CLASSES:
            with self.subTest(cls=cls):
                d, r = outcome(L1, RECOMP, "The documents report that every number in this package is taken from "
                                           "narrative reports.", cls)
                self.assertEqual(d, "WITHHELD")
                self.assertTrue(any(x.startswith("CONTRAST_REVERSED") for x in r["reasons"]), r["reasons"])


class K02Safeguards(unittest.TestCase):
    """Constructed categories (development set): execution counts and frequency, reported vs recomputed and other
    provenance escalation, contrast orientation, inference added to a direct statement, comparison and temporal
    qualifiers added."""
    CASES = [
        ("count: once -> three times", R2, RAN_ONCE,
         "The documents report that the 9-qubit quantum-kernel experiment was run three times on IBM Quantum "
         "hardware.", "COUNT"),
        ("count: once -> repeatedly", R2, RAN_ONCE,
         "The documents report that the 9-qubit quantum-kernel experiment was run repeatedly on IBM Quantum hardware.",
         "COUNT"),
        ("count: single job -> several jobs", L1, SINGLE,
         "The documents report that the IBM baseline is several jobs: 5 circuits, 4096 shots each.", "COUNT"),
        ("count: one job -> two jobs", R8, ONE_JOB, "The documents report that they ran two jobs on ibm_marrakesh.",
         "COUNT"),
        ("count: only run -> one of several", R15, "This is the only hardware run.",
         "The documents report that this is one of several hardware runs on ibm_marrakesh.", "COUNT"),
        ("provenance: reported -> verified", R8, REPORTED,
         "The documents report that the hardware numbers below are verified results.", "PROVENANCE"),
        ("provenance: reported -> measured and confirmed", R8, REPORTED,
         "The hardware numbers below are measured and confirmed results.", "PROVENANCE"),
        ("provenance: not recomputable -> recomputed", R15, HW_ROW,
         "The documents report that the values measured on ibm_marrakesh are recomputed here.", "PROVENANCE"),
        ("contrast: instead of reversed", L1, RECOMP,
         "The documents report that every number in this package is taken from narrative reports instead of being "
         "recomputed by code in the repository.", "CONTRAST_REVERSED"),
        ("inference added: so", L1, FITS,
         "It fits the IBM data (χ² = 5.65 on 4 dof), so the noise mechanism is depolarising.", "INFERENCE"),
        ("inference added: which means", L1, DRIFT,
         "Calibration drift between days is not characterised for this workload, which means the results are "
         "unstable.", "INFERENCE"),
        ("comparison added", L1, DRIFT,
         "Calibration drift between days is not characterised for this workload, worse than for other workloads.",
         "COMPARISON"),
        ("temporal qualifier added", L1, SINGLE,
         "The documents report that the IBM baseline was previously a single job: 5 circuits, 4096 shots each.",
         "TEMPORAL"),
    ]

    def test_none_supported_under_any_class_and_reason_named(self):
        for name, anchor, quote, claim, code in self.CASES:
            with self.subTest(name=name):
                sup, d, why = best(anchor, quote, claim)
                self.assertEqual(sup, [], why)
                self.assertIn(code, why)

    def test_unconfirmed_is_the_floor(self):
        """A conclusion that cannot be established is UNCONFIRMED at best, never DOCUMENT_SUPPORTED."""
        for name, anchor, quote, claim, code in self.CASES:
            with self.subTest(name=name):
                d = outcome(anchor, quote, claim, "UNKNOWN_OR_UNSUPPORTED")[0]
                self.assertIn(d, ("UNCONFIRMED", "WITHHELD"))


class K03ControlsStaySupported(unittest.TestCase):
    CASES = [
        ("count kept: once", R2, RAN_ONCE,
         "The documents report that the 9-qubit quantum-kernel experiment was run once on IBM Quantum hardware.",
         "HARDWARE_EXECUTION"),
        ("count kept: one job = a single job", R8, "We ran one job on `ibm_marrakesh` (9 qubits, 5 circuits × 4,096 "
                                                   "shots = 20,480 shots, 660 CZ per submitted circuit).",
         "The documents report that one job was run on `ibm_marrakesh` with 5 circuits × 4,096 shots = 20,480 shots.",
         "HARDWARE_EXECUTION"),
        ("contrast kept", L1, RECOMP,
         "The documents report that every number in this package is recomputed by code in the repository rather "
         "than taken from narrative reports.", "RAW_EVIDENCE_AVAILABILITY"),   # class whose cue ("recomput") matches
        ("reported kept", R15, HW_ROW,
         "The documents report that the values measured on ibm_marrakesh are reported, not recomputable here.",
         "REPORTED_HARDWARE_RESULT"),
        ("real F1", PW, "**One** IBM job: the exact 9-qubit kernel circuit for 5 frozen kernel entries, 4096 shots each",
         "The project documents report that one IBM job ran the exact 9-qubit kernel circuit for 5 frozen kernel "
         "entries, at 4096 shots each.", "HARDWARE_EXECUTION"),
        ("real F3", R8, "Its raw job files and provider metadata are not in this repository until redistribution "
                        "rights are confirmed (`WITHHELD_EVIDENCE.md`).",
         "The raw IBM job files and provider metadata are not in the repository until redistribution rights are "
         "confirmed.", "RAW_EVIDENCE_AVAILABILITY"),
    ]

    def test_controls(self):
        for name, anchor, quote, claim, cls in self.CASES:
            with self.subTest(name=name):
                d, r = outcome(anchor, quote, claim, cls)
                self.assertEqual(d, "SUPPORTED", r)

    def test_real_output_replay_still_five_of_five(self):
        from autonomos.r1d.replay import replay_dir
        from test_r1e_unified import APP, CORPUS
        r = replay_dir(os.path.join(APP, "tests", "fixtures", "real_ai_test1"), CORPUS)
        self.assertEqual(r["verification"]["counts"]["claim_supported"], 5)


if __name__ == "__main__":
    unittest.main()
