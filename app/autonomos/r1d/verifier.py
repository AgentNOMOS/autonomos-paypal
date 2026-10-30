"""Deterministic evidence verification for R1d / R1d.2. Independent of the model: it sees the parsed model output, the
pinned collection loaded and re-hashed at execution time, and the exact passage excerpts that were shown to the model.
No language model is used to judge another model's claims.

Three separate levels per finding. The first never implies the second, and nothing implies the third:
  QUOTE_VERIFIED               the quotation occurs in the cited passage (rule R1D-QUOTE-1 under R1D-NORM-1, unchanged)
  CLAIM_SUPPORTED              the documents support the claim under rule R1D2-SUPPORT-1: the hard screen
                               (R1D2-SCREEN-1), the class check, the numeric check (R1D2-NUM-1) and lexical coverage
                               over the bounded evidence span (quotation + its R1D2-CTX-1 context unit). Deterministic
                               document support, NOT semantic entailment and NOT truth.
  TRUTH_INDEPENDENTLY_VERIFIED never assigned. Every finding is NOT_INDEPENDENTLY_VERIFIED and its independent_evidence
                               is NOT_ESTABLISHED: AutoNOMOS checks what the documents say, not whether the world
                               agrees with them, and it holds no raw hardware evidence.

R1d.2 context (R1D2-CTX-1): the context unit of a quotation is the smallest complete sentence run (prose / list item)
or the single table row together with that table's header row, taken from the SAME pinned passage of the SAME
authenticated document, that contains the whole quotation (at most 800 characters). Nothing is ever taken from another
passage or another document, and passages are never joined. The context unit may (a) establish an execution
statement (a completed-execution sentence, or a table row whose "hardware jobs" column records >= 1 job) for the
claim's platforms, (b) give table cells their column meaning for numbers, and (c) count for lexical coverage. Numbers
and identifiers in a claim must still occur in the quotation itself.

Decisions per finding:
  REJECTED  structure, finding limit, source membership, anchor resolution or quote check failed
  WITHHELD  the quote is real, but the claim fails a hard screen rule (unsupported, changed or misattributed number,
            interval, unit or qualifier; dropped or added negation; an execution, advantage, independent-verification
            or raw-evidence assertion the evidence span does not make; an execution contradicted in the cited passage)
            or the cited passage carries instruction-like text; the claim is not delivered
  DELIVERED quote verified; claim_status CLAIM_SUPPORTED or CLAIM_UNCONFIRMED (labelled; read the quote, not the claim)
Every finding carries `support`: the evidence span used, every check with its detail, and the reasons for acceptance
or rejection.
"""
import re

from ..corpus import STOP
from ..research import FORBIDDEN_OUTPUT_KEYS, _keys
from .engine import EVIDENCE_CLASSES, FINDING_KEYS, MAX_QUOTE, MAX_TEXT, MIN_QUOTE

VERIFIER_VERSION = "R1E1-VERIFIER-1"
CLAIM_RULE_ID = "R1E1-SUPPORT-1"
NORMALIZATION_RULE = ("R1D-NORM-1: runs of whitespace (Python regex \\s) collapse to one space and leading/trailing "
                      "whitespace is trimmed, on both sides. Nothing else: no case folding, no Unicode normalization, "
                      "no quote-mark, dash or ellipsis folding, no markdown stripping, no fragment joining.")
QUOTE_RULE = (f"R1D-QUOTE-1: the normalized quotation ({MIN_QUOTE}-{MAX_QUOTE} characters) is a contiguous substring "
              "of the normalized excerpt of the cited passage that was shown to the model; the passage belongs to the "
              "approved collection whose bytes were re-hashed at execution time and match the offer.")
CONTEXT_RULE = ("R1D2-CTX-1: the context unit is the smallest complete sentence run (prose or one list item) or the "
                "one table row plus its table's header row, inside the cited passage of the same document, that "
                "contains the whole quotation; at most 800 characters; never another passage or document. It may "
                "establish an execution statement for the claim's platforms, give table cells their column meaning "
                "and count for lexical coverage. Numbers and identifiers must still occur in the quotation.")
SCREEN_RULE = ("R1D2-SCREEN-1 (hard; failure = WITHHELD): every number and every alphanumeric identifier in the claim "
               "occurs in the quotation; the claim and the quotation agree on negation (both or neither contain a "
               "negation word); a claim that asserts a completed execution on a platform needs a completed-execution "
               "statement for that platform (or a more general one naming it) in the quotation or its context unit, "
               "without negation, cancellation or plan wording, and no sentence or row of the cited passage may deny "
               "an execution on that platform (R1D2-CONTRA-1); a claim of quantum advantage, of independent "
               "verification or of available raw evidence needs a quotation that asserts the same without negation; "
               "the numeric rule R1D2-NUM-1 passes; the cited passage carries no instruction-like text.")
NUMERIC_RULE = ("R1D2-NUM-1 (hard): values are compared as written (no rounding); every confidence interval or +/- "
                "range in the claim occurs in the quotation with the same value, level and bounds in the same order; "
                "a unit the claim attaches to a number must be the unit the quotation attaches to it, and a percent "
                "sign may not be dropped; a comparison or approximation qualifier (above, at least, about, ~, <, ...) "
                "must be the same as in the quotation; in a table row a value cell of a reference column (exact, "
                "noise-free, simulated, classical, ...) must be described with a reference word and a value from "
                "another column must not be. A value whose interval the claim omits leaves the claim unconfirmed.")
CLASS_RULE = ("R1D2-CLASS-1: the model's evidence class is confirmed if the quotation contains that class's cue words "
              "(table in verifier.CLASS_CUES) or, for HARDWARE_EXECUTION, if the quotation's context unit "
              "establishes an execution statement; otherwise the class is shown as the model's proposal.")
LEX_RULE = ("R1E1-SUPPORT-1 (CLAIM_SUPPORTED): screen passed (including negation agreement, R1D2-NUM-1 and the hard "
            "parts of R1E-MEANING-1 and R1E1-TRANSFORM-1), no soft R1E-MEANING-1 or R1E1-TRANSFORM-1 reason, class confirmed, and at least 80% of the claim's content words (stop and framing words removed, light suffix "
            "stemming) occur in the evidence span: the quotation, plus - from its context unit only - the table's "
            "column names and row label, and the execution words and platforms of an established execution "
            "statement. Deterministic document support, not semantic entailment.")
MEANING_RULE = ("R1E-MEANING-1 (non-numeric meaning, deterministic; no language model): HARD (withheld) - a claim may "
                "not turn a question in its evidence span into a finding (QUESTION_PRESENTED_AS_FINDING), may not use "
                "a word whose listed antonym the quotation uses instead (POLARITY_FLIP: on/off, public/private, "
                "used/bypassed, reported/recomputed, ...), and may not name a platform or organisation absent from the "
                "quotation, its context unit and - for a quotation opening with a pronoun - the one preceding sentence "
                "(ENTITY_NOT_IN_EVIDENCE). SOFT (claim unconfirmed) - what a negation governs must match "
                "(NEGATION_SCOPE_CHANGED), no strength or scope words the quotation lacks (all, every, anyone, proves, "
                "demonstrates, correct, ...: STRENGTH_ESCALATION), and a condition (if, unless, assuming) or plan "
                "(plans, proposed, will) in the quotation must stay marked in the claim (CONDITION_DROPPED, "
                "PLAN_DROPPED).")
TRANSFORM_RULE = ("R1E1-TRANSFORM-1 (deterministic; no language model): HARD (withheld) - an execution count or "
                  "frequency word in the claim (once, twice, single, two, several, repeatedly, ...) that conflicts "
                  "with the count words of the evidence span (COUNT_CHANGED); a provenance upgrade (recomputed, "
                  "verified, confirmed, measured, reproduced, ...) of a statement the source frames as reported, "
                  "stated, claimed or narrative (PROVENANCE_ESCALATION); a contrast ('X rather than / instead of / "
                  "as opposed to Y', 'X, not Y', 'not Y but X') whose sides the claim reverses or whose rejected side "
                  "it asserts (CONTRAST_REVERSED). SOFT (claim unconfirmed, finding_basis INFERRED or TRANSFORMED) - "
                  "a count word absent from the evidence (COUNT_NOT_IN_EVIDENCE), any other provenance word absent "
                  "from the evidence (PROVENANCE_NOT_IN_EVIDENCE), an inference or causal connective absent from it "
                  "(therefore, so, which means, because, due to, ...: INFERENCE_NOT_IN_QUOTE), a comparison "
                  "(COMPARISON_NOT_IN_QUOTE) or temporal qualifier (TEMPORAL_NOT_IN_QUOTE) absent from it, a contrast "
                  "the quotation does not draw (CONTRAST_NOT_IN_QUOTE).")
TRUTH_NOTE = "NOT_INDEPENDENTLY_VERIFIED: the finding reports what the named document states."
INDEPENDENT_EVIDENCE_NOTE = ("NOT_ESTABLISHED: document support is not independent evidence. AutoNOMOS holds no raw "
                             "hardware data or provider job records for these documents.")
CLASS_CEILINGS = {
    "HARDWARE_EXECUTION": "Document-reported execution. AutoNOMOS did not see provider job records; not independently "
                          "verified.",
    "REPORTED_HARDWARE_RESULT": "The documents report this result, but raw hardware measurements were not provided to "
                                "AutoNOMOS; not independently verified.",
    "RAW_EVIDENCE_AVAILABILITY": "Availability as described by the documents; AutoNOMOS did not inspect the evidence.",
    "CLASSICAL_SIMULATION_OR_EXACT": "Documents describe a recomputable classical result; AutoNOMOS did not recompute it.",
    "COMPILED_ONLY": "Compiled-only per the documents; no hardware execution is implied.",
    "PLANNED_NOT_PERFORMED": "Planned or proposed per the documents; not performed.",
    "UNKNOWN_OR_UNSUPPORTED": "Stated by the documents as unknown, unconfirmed or not claimed.",
}

NUM = re.compile(r"\d+(?:[.,]\d+)*")
WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
NEG = {"no", "not", "never", "none", "nothing", "without", "withheld", "cannot", "unverified", "unconfirmed",
       "unavailable", "unknown", "absent", "missing", "neither", "nor", "lack", "lacks", "nobody", "undocumented"}
FRAME = {"document", "documents", "doc", "docs", "state", "states", "stated", "report", "reports", "reported",
         "project", "authors", "author", "according", "repository", "package", "say", "says", "said", "describe",
         "describes", "described", "note", "notes", "noted", "indicate", "indicates", "indicated", "fixture",
         "that", "there", "here", "also", "both", "which", "its", "it", "they", "their", "this",
         # R1d.2: comparison connectives carry no content of their own
         "compared", "versus", "vs", "relative", "against"}
PLATFORM = re.compile(r"\b(ibm_marrakesh|ionq|forte|trapped[- ]ion|quantinuum|braket|azure quantum|qutech|tuna-9|"
                      r"ibm|marrakesh|qollab)\b", re.I)
GENERIC_HW = re.compile(r"\b(quantum hardware|real hardware|hardware|qpu|quantum computer|device)\b", re.I)
COMPLETED = re.compile(r"\b(ran|executed|performed|measured|conducted|completed|obtained|achieved)\b|"
                       r"\b(was|were|has been|have been|had been) (run|executed|performed|measured|submitted|tested)\b",
                       re.I)
PLAN = re.compile(r"\b(if|plan|plans|planned|proposed|proposes|propose|would|will|future|intend|intends|intended)\b",
                  re.I)
# R1d.2: the same plan words, but not inside a hyphenated compound ("free-plan QPU time" is not a plan)
PLAN2 = re.compile(r"(?<![-\w])(if|plan|plans|planned|proposed|proposes|propose|would|will|future|intend|intends|"
                   r"intended)(?![-\w])", re.I)
ADVANTAGE = re.compile(r"quantum advantage|supremacy|outperform|beats? (a |the )?(tuned )?classical|"
                       r"better than (a |the )?(tuned )?classical|quantum\s*>\s*classical", re.I)
INDEP = re.compile(r"independently (verified|confirmed|reproduced|validated)|third[- ]party (verified|verification|"
                   r"confirmed)|(has|have|had) been (independently )?(verified|confirmed|validated)|"
                   r"(is|are|was|were) (independently )?(verified|confirmed|validated)\b", re.I)
RAW_AVAIL = re.compile(r"\braw\b[^.;]{0,60}\b(available|published|included|provided|released|public|accessible|"
                       r"downloadable|supplied)\b|independently verifiable|available for (independent )?"
                       r"(verification|recomputation)", re.I)
EXEC_WORD = r"(ran|run|runs|executed|execution|performed|submitted|measured)"
EXEC_DENIED = re.compile(r"(\b(not|never|no|neither|nor|cannot|without|cancel(?:l)?ed|failed|aborted)\b|n't\b)"
                         r"(\W+\w+){0,3}?\W+" + EXEC_WORD + r"\b", re.I)
EXEC_NEGATOR = re.compile(r"\b(cancel(?:l)?ed|failed|aborted|rejected|errored)\b", re.I)
JOBS_COLUMN = re.compile(r"hardware jobs?|qpu jobs?|jobs? on hardware|hardware runs?", re.I)
EXEC_TERMS = {"ran", "run", "executed", "execut", "performed", "perform", "measured", "measur", "conducted", "conduct",
              "completed", "complet"}
PLATFORM_FAMILY = {"ibm_marrakesh": {"ibm_marrakesh", "ibm"}, "marrakesh": {"ibm_marrakesh", "ibm"},
                   "forte": {"forte", "ionq"}, "tuna-9": {"tuna-9", "qutech"}, "trapped-ion": {"trapped-ion"}}
MAX_CONTEXT = 800
ANAPHOR = re.compile(r"^\W*(it|its|this|these|they|their|them|that|those|neither|both|such|the latter|the former)\b", re.I)
QUESTION_FRAME = re.compile(r"\?|\b(ask|asks|asked|asking|question|questions|whether)\b", re.I)
CONDITIONAL = re.compile(r"\b(if|unless|assuming|suppose|supposing|hypothetically)\b", re.I)
CONDITION_FRAME = re.compile(r"\b(if|unless|assum\w*|suppos\w*|hypothetical\w*|conditional\w*|illustrat\w*|would|could|"
                             r"might)\b", re.I)
PLAN_WORDS = re.compile(r"(?<![-\w])(plan|plans|planned|planning|proposed|proposes|propose|proposal|intend|intends|"
                        r"intended|future|will)(?![-\w])", re.I)
PLAN_FRAME = re.compile(r"\b(plan\w*|propos\w*|intend\w*|future|will|would|preregister\w*|not yet|scheduled)\b", re.I)
STRONG = {"all", "every", "everyone", "anyone", "always", "entire", "entirely", "full", "fully", "complete", "completely",
          "total", "totally", "prove", "proves", "proved", "proven", "demonstrate", "demonstrates", "demonstrated",
          "establish", "establishes", "established", "conclusive", "conclusively", "definitive", "definitively",
          "certain", "certainly", "clearly", "significant", "significantly", "guarantee", "guarantees", "guaranteed",
          "correct", "accurate", "true", "valid", "best", "superior", "definitely", "undoubtedly"}
POLAR_PAIRS = [("on", "off"), ("enabled", "disabled"), ("enable", "disable"), ("used", "bypassed"), ("use", "bypass"),
               ("uses", "bypasses"), ("public", "private"), ("public", "withheld"), ("available", "withheld"),
               ("available", "unavailable"), ("present", "absent"), ("included", "excluded"), ("accepted", "rejected"),
               ("passed", "failed"), ("success", "failure"), ("succeeded", "failed"), ("increased", "decreased"),
               ("increase", "decrease"), ("higher", "lower"), ("more", "less"), ("more", "fewer"),
               ("greater", "smaller"), ("above", "below"), ("before", "after"), ("maximum", "minimum"),
               ("best", "worst"), ("true", "false"), ("valid", "invalid"), ("consistent", "inconsistent"),
               ("supported", "unsupported"), ("confirmed", "unconfirmed"), ("verified", "unverified"),
               ("recomputed", "reported"), ("recomputable", "reported"), ("measured", "simulated"),
               ("real", "simulated"), ("noisy", "noiseless"), ("open", "closed"), ("required", "optional"),
               ("possible", "impossible"), ("competitive", "uncompetitive"), ("preserved", "destroyed"),
               ("preserves", "destroys"), ("retained", "lost"), ("survived", "lost")]
ANTONYMS = {}
for _a, _b in POLAR_PAIRS:
    ANTONYMS.setdefault(_a, set()).add(_b)
    ANTONYMS.setdefault(_b, set()).add(_a)
COUNT_WORDS = {"once": 1, "twice": 2, "thrice": 3, "single": 1, "sole": 1, "one": 1, "two": 2, "three": 3,
               "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "multiple": "MANY",
               "several": "MANY", "many": "MANY", "repeated": "MANY", "repeatedly": "MANY", "numerous": "MANY",
               "various": "MANY", "replicated": "MANY", "replicates": "MANY", "again": "MANY"}
REPORTED_FRAME = re.compile(r"\b(reported|reports|stated|states|claimed|claims|narrative|documented|as reported|"
                            r"according to)\b", re.I)
PROVENANCE = {"recomputed", "recomputable", "recompute", "reproduced", "reproducible", "verified", "verifiable",
              "confirmed", "validated", "measured", "certified", "audited", "replicated", "proven", "independently"}
INFERENCE = re.compile(r"\b(therefore|thus|hence|so|consequently|accordingly|which means|meaning that|implies|implying|"
                       r"indicates that|suggests that|shows that|demonstrates that|proves that|because|due to|caused by|"
                       r"leads? to|led to|as a result|resulting in)\b", re.I)
COMPARISON = {"more", "less", "fewer", "higher", "lower", "better", "worse", "best", "worst", "most", "least",
              "greater", "smaller", "larger", "faster", "slower", "improved", "improves", "degraded", "degrades",
              "increased", "decreased", "exceeds", "exceeded", "outperforms", "outperformed", "superior", "inferior"}
TEMPORAL = re.compile(r"\b(already|previously|formerly|no longer|anymore|still|currently|now|recently|soon|eventually|"
                      r"later|earlier|initially|originally)\b", re.I)
CONTRAST_CONNECTIVES = ("rather than", "instead of", "as opposed to", "in place of")
SCOPE_SKIP = {"whether", "only", "yet", "even", "just", "been", "being", "be", "ever", "also", "any"}

# numeric analysis (R1D2-NUM-1)
NUM_OCC = re.compile(r"(?<![A-Za-z0-9_.,])(\d+(?:[.,]\d+)*)(?![A-Za-z0-9_]|[.,]\d)")
TOK2 = re.compile(r"[A-Za-z0-9_]+(?:[.,][A-Za-z0-9_]+)*")
UNITS = {"%": "percent", "percent": "percent", "shot": "shots", "shots": "shots", "qubit": "qubits",
         "qubits": "qubits", "s": "seconds", "sec": "seconds", "second": "seconds", "seconds": "seconds",
         "ms": "milliseconds", "us": "microseconds", "μs": "microseconds", "cz": "cz", "circuit": "circuits",
         "circuits": "circuits", "entry": "entries", "entries": "entries", "job": "jobs", "jobs": "jobs",
         "usd": "usd", "dof": "dof", "pairs": "pairs", "pair": "pairs", "layers": "layers", "gates": "gates"}
QUALIFIER = re.compile(r"(more than|greater than|at least|over|above|exceed(?:s|ing)?|less than|fewer than|at most|"
                       r"up to|under|below|approximately|approx\.?|about|around|roughly|nearly|almost|~|≈|<=|>=|≤|≥|<|>)"
                       r"\s*$", re.I)
QUALIFIER_CLASS = {"more than": "GT", "greater than": "GT", "at least": "GT", "over": "GT", "above": "GT",
                   "exceed": "GT", "exceeds": "GT", "exceeding": "GT", ">": "GT", ">=": "GT", "≥": "GT",
                   "less than": "LT", "fewer than": "LT", "at most": "LT", "up to": "LT", "under": "LT", "below": "LT",
                   "<": "LT", "<=": "LT", "≤": "LT"}
CI = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:CI|confidence interval)\s*:?\s*\[?\(?\s*(\d+(?:[.,]\d+)*)\s*(?:–|—|-|to)\s*"
                r"(\d+(?:[.,]\d+)*)", re.I)
PLUSMINUS = re.compile(r"(\d+(?:[.,]\d+)*)\s*(?:±|\+/-)\s*(\d+(?:[.,]\d+)*)")
VALUE_CELL = re.compile(r"^\s*[~≈<>≤≥]?\s*[-+]?\d")             # a table cell that is a value, not prose
REFERENCE = re.compile(r"exact|noise-?free|noiseless|ideal|simulat|classical|reference|theor", re.I)
CLAUSE = re.compile(r", |; |\bcompared (?:with|to)\b|\bversus\b|\bvs\.?(?=\s)|\bwhile\b|\bwhereas\b|\brelative to\b",
                    re.I)
SENT_END = re.compile(r"(?<=[.!?])(\*\*|\*|`)?(\s+)(?=[*`(\[\"'A-Z0-9])")
ITEM_START = re.compile(r"^\s*([*+-]|\d+[.)])\s+")


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


def numbers(s):
    return {n.replace(",", "") for n in NUM.findall(s)}


def numbers2(s):
    """R1d.2: numbers that stand alone (digits inside identifiers such as job ids or P0 are identifiers)."""
    return {m.group(1).replace(",", "") for m in NUM_OCC.finditer(s)}


def identifiers(s):
    return {t.lower() for t in TOK2.findall(s) if re.search(r"\d", t) and re.search(r"[A-Za-z]", t)}


def words(s):
    return WORD.findall(s.lower())


def has_neg(s):
    return any(t in NEG or t.endswith("n't") for t in words(s))


def _stem(t):
    for suf in ("ing", "ed", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            return t[: -len(suf)]
    return t


def content_stems(s):
    return {_stem(t) for t in words(s) if t not in STOP and t not in FRAME and not t.isdigit() and len(t) > 1}


def platforms(s):
    return {m.lower().replace(" ", "-") for m in PLATFORM.findall(s)}


def expand(ps):
    out = set()
    for p in ps:
        out |= PLATFORM_FAMILY.get(p, {p})
    return out


def _strip_md(s):
    return s.replace("**", "").replace("`", "").replace("*", "")


CLASS_CUES = {
    "HARDWARE_EXECUTION": lambda q: bool((PLATFORM.search(q) or GENERIC_HW.search(q)) and COMPLETED.search(q)
                                         and not has_neg(q) and not PLAN.search(q)),
    "REPORTED_HARDWARE_RESULT": lambda q: bool(numbers(q) and re.search(
        r"ibm|marrakesh|hardware|qpu|measured|self-overlap|shots|surviving contrast|dynamic range", q, re.I)),
    "RAW_EVIDENCE_AVAILABILITY": lambda q: bool(re.search(
        r"\braw\b|withheld|job files|provider metadata|recomput|evidence", q, re.I)),
    "CLASSICAL_SIMULATION_OR_EXACT": lambda q: bool(re.search(
        r"exact|simulat|classical|statevector|noise-free|noiseless|numpy", q, re.I)),
    "COMPILED_ONLY": lambda q: bool(re.search(
        r"compil|never submitted|never (been )?(run|ran|executed)|not (been )?(run|executed)|COMPILED-ONLY", q, re.I)),
    "PLANNED_NOT_PERFORMED": lambda q: bool(PLAN.search(q) or re.search(
        r"no ionq job|has been run|not yet|never submitted|PROPOSED", q, re.I)),
    "UNKNOWN_OR_UNSUPPORTED": lambda q: bool(re.search(
        r"unknown|unconfirmed|unverified|not documented|undocumented|cannot|not (yet )?(confirmed|claimed)|"
        r"\bno\b.{0,60}\b(claimed|exists?|result)|withheld|unresolved|neither", q, re.I)),
}


# ------------------------------------------------------------------------------------------------ context (R1D2-CTX-1)
def _cells(line):
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") else s
    return [norm(c) for c in s.split("|")]


def sentences(text):
    """Split normalized text into sentences without losing characters: " ".join(sentences(t)) == norm(t)."""
    t, out, start = norm(text), [], 0
    for m in SENT_END.finditer(t):
        cut = m.start(2)
        out.append(t[start:cut])
        start = m.end(2)
    out.append(t[start:])
    return [s for s in out if s]


def _structure(raw):
    """(tables, prose_items) of one raw passage. tables: [(header_line, [row_lines])]; items: [normalized text]."""
    lines = raw.split("\n")
    tables, items, i, cur = [], [], 0, None
    while i < len(lines):
        ln = lines[i]
        if ln.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
            rows, j = [], i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append(lines[j])
                j += 1
            tables.append((ln, rows))
            cur, i = None, j
            continue
        if not ln.strip():
            cur, i = None, i + 1
            continue
        if cur is None or ITEM_START.match(ln):
            items.append(ln)
            cur = len(items) - 1
        else:
            items[cur] += "\n" + ln
        i += 1
    return tables, [norm(x) for x in items]


def context_unit(docset, anchor, nq):
    """The R1D2-CTX-1 context unit of quotation nq in passage `anchor`, or None. Same passage only."""
    raw = getattr(docset, "raw_passages", {}).get(anchor)
    if raw is None or norm(raw) != docset.passages.get(anchor):
        return None
    tables, items = _structure(raw)
    for header, rows in tables:
        hcells = _cells(header)
        for r in rows:
            if nq in norm(r):
                text = norm(header) + " " + norm(r)
                if len(text) > MAX_CONTEXT:
                    return None
                cells = _cells(r)
                return {"kind": "table_row", "passage_id": anchor, "header": norm(header), "text": norm(r),
                        "header_cells": hcells, "cells": cells, "label": cells[0] if cells else ""}
    for item in items:
        if nq not in item:
            continue
        ss = sentences(item)
        for n in range(1, len(ss) + 1):
            for a in range(0, len(ss) - n + 1):
                text = " ".join(ss[a:a + n])
                if nq in text:
                    if len(text) > MAX_CONTEXT:
                        return None
                    ante = ss[a - 1] if a > 0 and ANAPHOR.match(text) else None
                    return {"kind": "sentence", "passage_id": anchor, "header": None, "text": text,
                            "header_cells": None, "cells": None, "label": None, "antecedent": ante}
    return None


def _all_units(docset, anchor):
    raw = getattr(docset, "raw_passages", {}).get(anchor)
    if raw is None:
        return [docset.passages.get(anchor, "")]
    tables, items = _structure(raw)
    return [norm(r) for _, rows in tables for r in rows] + [s for it in items for s in sentences(it)]


def unit_execution(unit):
    """(established, platforms, how) for a context unit: a completed-execution sentence, or a table row whose
    hardware-jobs column records >= 1 job, with no negation, cancellation or plan wording in the unit."""
    if unit is None:
        return False, set(), None
    text = unit["text"]
    if has_neg(text) or PLAN2.search(text) or EXEC_NEGATOR.search(text):
        return False, set(), None
    if unit["kind"] == "table_row":
        for j, h in enumerate(unit["header_cells"]):
            if JOBS_COLUMN.search(h) and j < len(unit["cells"]):
                m = re.match(r"\s*(\d+)\b", _strip_md(unit["cells"][j]))
                if m and int(m.group(1)) >= 1:
                    return True, platforms(text), f"table row: column '{h}' records {m.group(1)} job(s)"
        return False, set(), None
    if COMPLETED.search(text) and (PLATFORM.search(text) or GENERIC_HW.search(text)):
        return True, platforms(text), "sentence states a completed execution"
    return False, set(), None


def execution_denials(docset, anchor):
    """R1D2-CONTRA-1: sentences / rows of the cited passage that deny an execution: [(text, platforms or None)]."""
    out = []
    for u in _all_units(docset, anchor):
        if EXEC_DENIED.search(u):
            out.append((u, platforms(u) or None))
    return out


def _platform_ok(claim_ps, evidence_ps):
    return claim_ps <= expand(evidence_ps)


# ------------------------------------------------------------------------------------------------ numbers (R1D2-NUM-1)
def _occurrences(text):
    t = _strip_md(text)
    out = []
    for m in NUM_OCC.finditer(t):
        v, after, before = m.group(1).replace(",", ""), t[m.end():], t[:m.start()]
        um = re.match(r"\s*-?\s*(%|[A-Za-zμ]+)", after)
        unit = UNITS.get(um.group(1).lower()) if um else None
        if re.search(r"(USD|US\$|\$)\s*$", before):
            unit = "usd"
        qm = QUALIFIER.search(before[-25:])
        q = None
        if qm:
            k = qm.group(1).lower().rstrip(".")
            q = QUALIFIER_CLASS.get(k, "APPROX")
        out.append({"value": v, "unit": unit, "qualifier": q, "start": m.start(), "end": m.end()})
    return t, out


def intervals(text):
    t = _strip_md(text)
    out = []
    for m in CI.finditer(t):
        prev = [x.group(1) for x in NUM_OCC.finditer(t[max(0, m.start() - 30):m.start()])]
        out.append({"value": prev[-1].replace(",", "") if prev else None, "level": m.group(1),
                    "low": m.group(2).replace(",", ""), "high": m.group(3).replace(",", "")})
    for m in PLUSMINUS.finditer(t):
        out.append({"value": m.group(1).replace(",", ""), "level": "±", "low": m.group(2).replace(",", ""),
                    "high": m.group(2).replace(",", "")})
    return out


def _clause_at(text, pos):
    cuts = [0] + [m.end() for m in CLAUSE.finditer(text) if m.end() <= pos] + [len(text)]
    start = max(c for c in cuts if c <= pos)
    nxt = [m.start() for m in CLAUSE.finditer(text) if m.start() > pos]
    return text[start:nxt[0] if nxt else len(text)]


def numeric_check(claim, quote, unit):
    """R1D2-NUM-1. Returns (hard_reasons, soft_reasons, info)."""
    hard, soft = [], []
    ct, cocc = _occurrences(claim)
    qt, qocc = _occurrences(quote)
    info = {"status": "NO_NUMBERS", "values": sorted({o["value"] for o in cocc}), "intervals": [], "bindings": []}
    if not cocc:
        return hard, soft, info
    qby = {}
    for o in qocc:
        qby.setdefault(o["value"], []).append(o)
    for o in cocc:
        qs = qby.get(o["value"])
        if not qs:
            continue                                   # reported by the number screen (NUMBER_NOT_IN_QUOTE)
        qunits = {x["unit"] for x in qs}
        if o["unit"] and o["unit"] not in qunits:
            if qunits - {None}:
                hard.append(f"UNIT_MISMATCH {o['value']}: claim '{o['unit']}', quotation {sorted(qunits - {None})}")
            elif not (unit and unit.get("header") and o["unit"] in {UNITS.get(w) for w in words(unit["header"])}):
                soft.append(f"UNIT_NOT_IN_EVIDENCE {o['value']} '{o['unit']}'")
        if "percent" in qunits and None not in qunits and o["unit"] != "percent":
            hard.append(f"PERCENT_DROPPED {o['value']}")
        qquals = {x["qualifier"] for x in qs}
        if o["qualifier"] not in qquals:
            hard.append(f"QUALIFIER_CHANGED {o['value']}: claim {o['qualifier'] or 'none'}, quotation "
                        f"{sorted(q or 'none' for q in qquals)}" if o["qualifier"] else
                        f"QUALIFIER_DROPPED {o['value']}: quotation {sorted(q or 'none' for q in qquals)}")
    civ, qiv = intervals(claim), intervals(quote)
    keyed = [(i["value"], i["level"], i["low"], i["high"]) for i in qiv]
    for i in civ:
        if (i["value"], i["level"], i["low"], i["high"]) not in keyed:
            hard.append(f"INTERVAL_MISMATCH claim {i['value']} ({i['level']} {i['low']}-{i['high']}) not in the "
                        f"quotation {[k for k in keyed]}")
        else:
            info["intervals"].append(i)
    claimed_vals = {o["value"] for o in cocc}
    for i in qiv:
        if i["value"] in claimed_vals and not any(c["value"] == i["value"] for c in civ):
            soft.append(f"UNCERTAINTY_DROPPED {i['value']} ({i['level']} {i['low']}-{i['high']})")
    in_interval = {x for i in civ for x in (i["level"], i["low"], i["high"])}
    if unit and unit["kind"] == "table_row":
        hc, cells = unit["header_cells"], [_strip_md(c) for c in unit["cells"]]
        for o in cocc:
            v = o["value"]
            cols = [j for j, c in enumerate(cells) if v in numbers2(c)]
            if not cols or v in in_interval and v not in {i["value"] for i in civ}:
                continue
            window = _clause_at(ct, o["start"])
            ok_any, rec = False, None
            for j in cols:
                if j == 0 or not VALUE_CELL.match(cells[j]):     # row label or descriptive text: meaning is in the quote
                    ok_any, rec = True, {"number": v, "column": _strip_md(hc[j]) if j < len(hc) else "",
                                         "row": cells[0], "reference_column": False, "consistent": True,
                                         "cell": "label" if j == 0 else "descriptive"}
                    break
                h = _strip_md(hc[j]) if j < len(hc) else ""
                is_ref = bool(REFERENCE.search(h))
                cue = bool(REFERENCE.search(window))
                ok = cue if is_ref else (not cue or bool(content_stems(h) & content_stems(window)))
                rec = {"number": v, "column": h, "row": cells[0], "reference_column": is_ref, "consistent": ok,
                       "cell": "value"}
                if ok:
                    ok_any = True
                    break
            info["bindings"].append(rec)
            if not ok_any:
                hard.append(f"NUMBER_MISATTRIBUTED {v}: in column '{rec['column']}' of row '{rec['row']}', but the "
                            f"claim describes it as {'a hardware' if rec['reference_column'] else 'a reference'} value")
    info["status"] = "NUMBERS_MATCH" if not hard and not soft else "NUMBER_PROBLEMS"
    return hard, soft, info


# ------------------------------------------------------------------------------------------------ screens
def screen_claim(claim, quote, unit=None, denials=()):
    """Hard rules (R1D2-SCREEN-1). Returns (reasons, notes); any reason => WITHHELD."""
    reasons, notes = [], []
    missing = sorted(numbers2(claim) - numbers2(quote))
    if missing:
        reasons.append(f"NUMBER_NOT_IN_QUOTE {missing}")
    missing_ids = sorted(identifiers(claim) - identifiers(quote))
    if missing_ids:
        reasons.append(f"IDENTIFIER_NOT_IN_QUOTE {missing_ids}")
    if has_neg(claim) != has_neg(quote):
        reasons.append("NEGATION_MISMATCH")
    if COMPLETED.search(claim) and (PLATFORM.search(claim) or GENERIC_HW.search(claim)) and not has_neg(claim):
        if not (COMPLETED.search(quote) and not has_neg(quote) and not PLAN.search(quote)
                and _platform_ok(platforms(claim), platforms(quote))):
            est, ps, how = unit_execution(unit)
            if est and _platform_ok(platforms(claim), ps):
                notes.append(f"EXECUTION_FROM_CONTEXT_UNIT ({how}; platforms {sorted(ps) or ['generic hardware']})")
            else:
                reasons.append("EXECUTION_ASSERTION_NOT_IN_QUOTE")
        cps = expand(platforms(claim))
        for text, dps in denials:
            if dps is None or (cps and cps & expand(dps)):
                reasons.append(f"CONTRADICTED_BY_SOURCE_CONTEXT: '{text[:160]}'")
                break
    if ADVANTAGE.search(claim) and not has_neg(claim) and not (ADVANTAGE.search(quote) and not has_neg(quote)):
        reasons.append("ADVANTAGE_ASSERTION_NOT_IN_QUOTE")
    if INDEP.search(claim) and not has_neg(claim) and not (INDEP.search(quote) and not has_neg(quote)):
        reasons.append("INDEPENDENT_VERIFICATION_ASSERTION_NOT_IN_QUOTE")
    if RAW_AVAIL.search(claim) and not has_neg(claim) and not (RAW_AVAIL.search(quote) and not has_neg(quote)):
        reasons.append("RAW_EVIDENCE_ASSERTION_NOT_IN_QUOTE")
    return reasons, notes


def _negated_heads(s, k):
    toks, out = words(s), []
    for i, t in enumerate(toks):
        if t in NEG or t.endswith("n't"):
            heads = []
            for u in toks[i + 1:]:
                if u in NEG or u in STOP or u in FRAME or u in SCOPE_SKIP or u.isdigit() or len(u) < 2:
                    continue
                heads.append(_stem(u))
                if len(heads) == k:
                    break
            out.append(set(heads))
    return out


def _counts(text):
    """(word-derived count values, integers written as digits) of a text."""
    ws = words(text)
    vals = {COUNT_WORDS[w] for i, w in enumerate(ws) if w in COUNT_WORDS        # a negated count ("no replicates") is
            and not any(x in NEG or x.endswith("n't") for x in ws[max(0, i - 2):i])}   # not a count
    vals |= {int(n) for n in re.findall(r"\b(\d+) times\b", text)}
    return vals, {int(n) for n in numbers2(text) if n.isdigit()}


def _contrast(text):
    """(preferred side, rejected side) of the first contrast construction, or None."""
    t = " " + _strip_md(text).lower() + " "
    for c in CONTRAST_CONNECTIVES:
        i = t.find(f" {c} ")
        if i >= 0:
            return t[:i], t[i + len(c) + 2:]
    m = re.search(r"\bnot\s+(.+?),?\s+but\s+(.+)", t)
    if m:
        return m.group(2), m.group(1)
    m = re.search(r",\s*not\s+(.+)", t)
    if m:
        return t[:m.start()], m.group(1)
    return None


def transform_check(claim, quote, unit=None):
    """R1E1-TRANSFORM-1. Returns (hard_reasons, soft_reasons)."""
    hard, soft = [], []
    span = " ".join(x for x in (quote, unit and unit.get("text")) if x)
    c_words, _ = _counts(claim)
    e_words, e_digits = _counts(span)
    for c in sorted(c_words, key=str):
        if c in e_words or (isinstance(c, int) and c in e_digits) or \
                (c == "MANY" and any(v == "MANY" or (isinstance(v, int) and v > 1) for v in e_words)):
            continue
        if e_words:
            hard.append(f"COUNT_CHANGED: the claim says {c!r}, the evidence says {sorted(e_words, key=str)}")
        else:
            soft.append(f"COUNT_NOT_IN_EVIDENCE: {c!r}")
    span_stems = {_stem(w) for w in words(span)}
    prov = sorted({w for w in words(claim) if w in PROVENANCE and _stem(w) not in span_stems})
    if prov:
        if REPORTED_FRAME.search(span):
            hard.append(f"PROVENANCE_ESCALATION {prov}: the source frames this as reported")
        else:
            soft.append(f"PROVENANCE_NOT_IN_EVIDENCE {prov}")
    qc, cc = _contrast(quote), _contrast(claim)
    if qc:
        qa, qb = content_stems(qc[0]), content_stems(qc[1])
        sa, sb = qa - qb, qb - qa
        if cc:
            ca, cb = content_stems(cc[0]), content_stems(cc[1])
            if ca & sb and cb & sa:
                hard.append("CONTRAST_REVERSED: the claim swaps the preferred and the rejected side of the quotation")
        else:
            cs = content_stems(claim)        # shared subject words sit on the preferred side; compare the proportions
            if cs & sb and len(cs & sb) / len(sb) > (len(cs & sa) / len(sa) if sa else 0):
                hard.append("CONTRAST_REVERSED: the claim asserts the side the quotation rejects")
    elif cc:
        soft.append("CONTRAST_NOT_IN_QUOTE: the claim draws a contrast the quotation does not")
    inf = sorted({m.group(1).lower() for m in INFERENCE.finditer(claim)} -
                 {m.group(1).lower() for m in INFERENCE.finditer(span)})
    if inf:
        soft.append(f"INFERENCE_NOT_IN_QUOTE {inf}: the claim infers beyond what the quotation states")
    comp = sorted((set(words(claim)) & COMPARISON) - set(words(span)))
    if comp:
        soft.append(f"COMPARISON_NOT_IN_QUOTE {comp}")
    temp = sorted({m.group(1).lower() for m in TEMPORAL.finditer(claim)} -
                  {m.group(1).lower() for m in TEMPORAL.finditer(span)})
    if temp:
        soft.append(f"TEMPORAL_NOT_IN_QUOTE {temp}")
    return hard, soft


def meaning_check(claim, quote, unit=None):
    """R1E-MEANING-1. Returns (hard_reasons, soft_reasons)."""
    hard, soft = [], []
    span = " ".join(x for x in (quote, unit and unit.get("text"), unit and unit.get("header"),
                                unit and unit.get("antecedent")) if x)
    if ("?" in quote or (unit and "?" in unit["text"])) and not QUESTION_FRAME.search(claim):
        hard.append("QUESTION_PRESENTED_AS_FINDING: the evidence span asks a question; the claim states an answer")
    cw, qw = set(words(claim)), set(words(quote))
    flips = sorted(f"{a}<>{b}" for a in cw - FRAME - qw for b in ANTONYMS.get(a, ()) if b in qw and b not in cw)
    if flips:
        hard.append(f"POLARITY_FLIP {flips}: the quotation says the opposite word")
    ev = span.lower().replace(" ", "-")
    missing = sorted(p for p in platforms(claim) if not any(n in ev for n in {p} | PLATFORM_FAMILY.get(p, set())))
    if missing:
        hard.append(f"ENTITY_NOT_IN_EVIDENCE {missing}: not named in the quotation or its context unit")
    q_heads = _negated_heads(quote, 4)
    for h in _negated_heads(claim, 2):
        if h and q_heads and not any(h & qh for qh in q_heads):
            soft.append(f"NEGATION_SCOPE_CHANGED: the claim negates {sorted(h)}, the quotation negates "
                        f"{sorted(set().union(*q_heads))}")
            break
    strong = sorted((cw & STRONG) - qw)
    if strong:
        soft.append(f"STRENGTH_ESCALATION {strong}: not in the quotation")
    if CONDITIONAL.search(quote) and not CONDITION_FRAME.search(claim):
        soft.append("CONDITION_DROPPED: the quotation is conditional; the claim is not")
    if PLAN_WORDS.search(quote) and not PLAN_FRAME.search(claim):
        soft.append("PLAN_DROPPED: the quotation describes a plan; the claim does not say so")
    return hard, soft


def screen_free_text(text, allowed_numbers, allowed_ids=None, exec_backing=None):
    """For the model's summary and not-established statements (no quotation backs them). Sentence by sentence: a
    negation in one sentence does not excuse an assertion in another. An execution assertion is backed only by a
    delivered finding whose evidence span establishes an execution on the same platforms."""
    reasons = []
    for s in sentences(text):
        if COMPLETED.search(s) and (PLATFORM.search(s) or GENERIC_HW.search(s)) and not has_neg(s):
            ps = platforms(s)
            if not any(_platform_ok(ps, b) for b in (exec_backing or [])):
                reasons.append("UNBACKED_EXECUTION_ASSERTION")
        if ADVANTAGE.search(s) and not has_neg(s):
            reasons.append("UNBACKED_ADVANTAGE_ASSERTION")
        if INDEP.search(s) and not has_neg(s):
            reasons.append("UNBACKED_INDEPENDENT_VERIFICATION_ASSERTION")
        if RAW_AVAIL.search(s) and not has_neg(s):
            reasons.append("UNBACKED_RAW_EVIDENCE_ASSERTION")
    extra = sorted(numbers2(text) - allowed_numbers)
    if extra:
        reasons.append(f"NUMBER_NOT_IN_ANY_VERIFIED_QUOTE {extra}")
    extra_ids = sorted(identifiers(text) - (allowed_ids or set()))
    if extra_ids:
        reasons.append(f"IDENTIFIER_NOT_IN_ANY_VERIFIED_EVIDENCE {extra_ids}")
    return list(dict.fromkeys(reasons))


def claim_support(claim, quote, cls, unit=None, exec_ps=None):
    """R1D2-SUPPORT-1 (soft part): class confirmation and lexical coverage over the bounded evidence span."""
    q_neg, c_neg = has_neg(quote), has_neg(claim)
    base = content_stems(quote)
    if unit and unit["kind"] == "table_row":
        base |= content_stems(" ".join(unit["header_cells"])) | content_stems(unit["label"] or "")
    if exec_ps is not None:
        base |= EXEC_TERMS | content_stems(" ".join(expand(exec_ps)).replace("_", " "))
    cs, qs = content_stems(claim), base
    cov = (len(cs & qs) / len(cs)) if cs else 0.0
    class_ok = CLASS_CUES[cls](quote) or (cls == "HARDWARE_EXECUTION" and exec_ps is not None)
    detail = {"lexical_coverage": round(cov, 3), "content_words": len(cs), "negation_parity": q_neg == c_neg,
              "class_confirmed": class_ok, "uncovered_words": sorted(cs - qs)}
    reasons = []
    if not class_ok:
        reasons.append("CLASS_CUE_ABSENT_IN_QUOTE")
    if len(cs) < 2 or cov < 0.8:
        reasons.append("LEXICAL_COVERAGE_BELOW_0.8")
    return ("CLAIM_SUPPORTED" if not reasons else "CLAIM_UNCONFIRMED"), reasons, detail


def _check_structure(f, seen_ids):
    if not isinstance(f, dict):
        return ["MALFORMED_FINDING: not an object"]
    r = []
    if set(f) != set(FINDING_KEYS):
        r.append(f"MALFORMED_FINDING: keys {sorted(f)}")
    for k in FINDING_KEYS:
        v = f.get(k)
        if not isinstance(v, str) or not v.strip():
            r.append(f"MISSING_OR_EMPTY_FIELD {k}")
        elif k in ("claim", "limitation") and len(v) > MAX_TEXT:
            r.append(f"FIELD_TOO_LONG {k}")
    if isinstance(f.get("evidence_class"), str) and f["evidence_class"] not in EVIDENCE_CLASSES:
        r.append("UNKNOWN_EVIDENCE_CLASS")
    fid = f.get("finding_id")
    if isinstance(fid, str):
        if len(fid) > 32:
            r.append("FIELD_TOO_LONG finding_id")
        if fid in seen_ids:
            r.append("DUPLICATE_FINDING_ID")
        seen_ids.add(fid)
    return r


def _span(docset, src, anchor, nq, unit):
    ctx = None
    if unit:
        ctx = {k: unit[k] for k in ("passage_id", "kind", "header", "text")}
        ctx.update(document=src, document_sha256=docset.files.get(src))
    return {"quote": {"passage_id": anchor, "document": src, "document_sha256": docset.files.get(src), "text": nq},
            "context": ctx}


def verify(raw, out, docset, offer, provided, execution_class):
    """Returns (deliverable_report, verification). Pure function of its inputs."""
    checks = []

    def chk(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return ok

    chk("bound_to_offer", raw.get("offer_id") == offer["offer_id"] and raw.get("task_id") == offer["task_id"]
        and raw.get("question") == offer["question"], "artifact names the paid offer, task and question")
    chk("document_set_matches_offer", raw.get("document_set") == offer["document_set"]
        and docset.manifest_digest == offer["document_set"]["manifest_digest"],
        "collection re-loaded and re-hashed at execution time equals the priced collection")
    bad_keys = sorted(_keys(out) & FORBIDDEN_OUTPUT_KEYS)
    chk("no_payment_fields_in_model_output", not bad_keys, f"forbidden keys present: {bad_keys}" if bad_keys else "")
    chk("passages_shown_belong_to_collection", all(p in docset.passages and docset.passages[p].startswith(t)
                                                   for p, t in provided.items()), "every excerpt is a pinned passage")
    flagged = set(raw.get("flagged_passages") or [])
    set_id, maxf = offer["document_set"]["id"], offer["max_findings"]
    delivered, removed, decisions, seen = [], [], [], set()
    findings = out.get("findings") if isinstance(out.get("findings"), list) else []
    for i, f in enumerate(findings):
        reasons = _check_structure(f, seen)
        if i >= maxf:
            reasons.append(f"EXCEEDS_FINDING_LIMIT ({maxf})")
        fid = f.get("finding_id") if isinstance(f, dict) and isinstance(f.get("finding_id"), str) else f"#{i + 1}"
        if not reasons:
            src, anchor, quote = f["source_document"], f["anchor"], f["quote"]
            nq = norm(quote)
            if src not in docset.files:
                reasons.append("SOURCE_NOT_IN_APPROVED_SET")
            if anchor not in docset.passages:
                reasons.append("ANCHOR_DOES_NOT_RESOLVE")
            elif not anchor.startswith(f"{set_id}/{src}#"):
                reasons.append("ANCHOR_SOURCE_MISMATCH")
            elif anchor not in provided:
                reasons.append("ANCHOR_NOT_SHOWN_TO_MODEL")
            if not reasons:
                if not MIN_QUOTE <= len(nq) <= MAX_QUOTE:
                    reasons.append(f"QUOTE_LENGTH_OUT_OF_RANGE ({len(nq)})")
                elif nq not in norm(provided[anchor]) or nq not in docset.passages[anchor]:
                    elsewhere = [p for p, t in docset.passages.items() if nq in t][:3]
                    reasons.append("QUOTE_NOT_FOUND_IN_CITED_PASSAGE" + (f" (occurs in {elsewhere})" if elsewhere
                                                                         else " (occurs nowhere in the collection)"))
        if reasons:
            removed.append({"finding_id": fid, "decision": "REJECTED", "reasons": reasons,
                            "claim": str(f.get("claim", ""))[:300] if isinstance(f, dict) else None,
                            "support": {"rule": CLAIM_RULE_ID, "rejected_because": reasons, "checks": []}})
            decisions.append({"finding_id": fid, "decision": "REJECTED", "reasons": reasons})
            continue
        claim, cls = f["claim"].strip(), f["evidence_class"]
        unit = context_unit(docset, anchor, nq)
        denials = execution_denials(docset, anchor)
        hard, notes = screen_claim(claim, nq, unit, denials)
        n_hard, n_soft, num_info = numeric_check(claim, nq, unit)
        m_hard, m_soft = meaning_check(claim, nq, unit)
        t_hard, t_soft = transform_check(claim, nq, unit)
        m_hard, m_soft = m_hard + t_hard, m_soft + t_soft
        hard += n_hard + m_hard
        if anchor in flagged:
            hard.append("SOURCE_PASSAGE_CONTAINS_INSTRUCTION_LIKE_TEXT")
        q_exec = bool(COMPLETED.search(nq) and not has_neg(nq) and not PLAN.search(nq)
                      and (PLATFORM.search(nq) or GENERIC_HW.search(nq)))
        u_est, u_ps, u_how = unit_execution(unit)
        exec_ps = platforms(nq) if q_exec else (u_ps if u_est else None)
        span = _span(docset, src, anchor, nq, unit)
        sup_checks = [
            {"check": "quote_verbatim_in_cited_passage", "ok": True, "detail": f"{anchor} (R1D-QUOTE-1)"},
            {"check": "context_unit", "ok": unit is not None,
             "detail": f"{unit['kind']} in {anchor}" if unit else "no bounded context unit; quotation only"},
            {"check": "execution_statement_in_span", "ok": exec_ps is not None,
             "detail": ("quotation" if q_exec else u_how) if exec_ps is not None else "none"},
            {"check": "numeric", "ok": not n_hard and not n_soft, "detail": num_info["status"]},
            {"check": "contradiction_scan", "ok": not any(r.startswith("CONTRADICTED") for r in hard),
             "detail": f"{len(denials)} execution denial(s) in the cited passage"},
            {"check": "meaning", "ok": not m_hard and not m_soft, "detail": "; ".join(m_hard + m_soft) or "R1E-MEANING-1"},
        ]
        if hard:
            sup = {"rule": CLAIM_RULE_ID, "evidence_span": span, "checks": sup_checks, "numeric": num_info,
                   "notes": notes, "rejected_because": hard, "accepted_because": []}
            removed.append({"finding_id": fid, "decision": "WITHHELD", "reasons": hard, "claim": claim[:300],
                            "quote_status": "QUOTE_VERIFIED", "anchor": anchor, "support": sup})
            decisions.append({"finding_id": fid, "decision": "WITHHELD", "reasons": hard,
                              "quote_status": "QUOTE_VERIFIED"})
            continue
        status, why, detail = claim_support(claim, nq, cls, unit, exec_ps)
        if n_soft:
            status, why = "CLAIM_UNCONFIRMED", why + n_soft
        if m_soft:
            status, why = "CLAIM_UNCONFIRMED", why + m_soft
        sup_checks.append({"check": "class_confirmed", "ok": detail["class_confirmed"], "detail": cls})
        sup_checks.append({"check": "lexical_coverage_over_span", "ok": "LEXICAL_COVERAGE_BELOW_0.8" not in why,
                           "detail": f"{detail['lexical_coverage']} of {detail['content_words']} content words; "
                                     f"uncovered {detail['uncovered_words']}"})
        accepted = [f"quotation verbatim in {anchor}"] + notes + (
            [f"numbers, intervals, units and qualifiers match ({', '.join(num_info['values'])})"]
            if num_info["status"] == "NUMBERS_MATCH" else []) + (
            [f"class {cls} confirmed", f"lexical coverage {detail['lexical_coverage']} >= 0.8 over the evidence span"]
            if status == "CLAIM_SUPPORTED" else [])
        sup = {"rule": CLAIM_RULE_ID, "evidence_span": span, "checks": sup_checks, "numeric": num_info,
               "notes": notes, "accepted_because": accepted, "rejected_because": [],
               "unconfirmed_because": why, "execution_platforms": sorted(exec_ps) if exec_ps is not None else None,
               # R1e.1: is the claim a direct restatement of the quotation, or an inference / transformation of it?
               "finding_basis": "INFERRED_OR_TRANSFORMED" if t_soft or m_soft else "DIRECT_STATEMENT"}
        label = (f"[QUOTE_VERIFIED · {status}" + (f" ({CLAIM_RULE_ID})" if status == "CLAIM_SUPPORTED" else
                                                  " - claim NOT confirmed, read the quotation") +
                 f" · {cls}{'' if detail['class_confirmed'] else ' (model-proposed class)'}]")
        delivered.append({
            "finding_id": fid, "claim": f"{label} {claim}", "claim_text": claim,
            "evidence": [{"passage_id": anchor, "quote": nq}], "source_document": src,
            "source_sha256": docset.files[src], "anchor": anchor, "evidence_class": cls,
            "class_confirmed": detail["class_confirmed"], "quote_status": "QUOTE_VERIFIED", "claim_status": status,
            "claim_rule": CLAIM_RULE_ID, "claim_unconfirmed_reasons": why, "support_detail": detail,
            "support": sup, "truth_status": "NOT_INDEPENDENTLY_VERIFIED", "independent_evidence": "NOT_ESTABLISHED",
            "source_provenance": {"document": src, "document_sha256": docset.files[src], "passage_id": anchor,
                                  "statement_type": "stated by the project document; AutoNOMOS did not observe it"},
            "verifier_note": CLASS_CEILINGS[cls], "model_limitation": f["limitation"].strip()[:MAX_TEXT]})
        decisions.append({"finding_id": fid, "decision": "DELIVERED", "claim_status": status, "reasons": why})
    chk("at_least_one_verified_finding", bool(delivered), f"{len(delivered)} delivered, {len(removed)} removed")

    spans = [d["evidence"][0]["quote"] for d in delivered] + [
        d["support"]["evidence_span"]["context"]["text"] for d in delivered if d["support"]["evidence_span"]["context"]]
    allowed_nums = set().union(*[numbers2(t) for t in spans]) if spans else set()
    allowed_ids = set().union(*[identifiers(t) for t in spans]) if spans else set()
    backing = [set(d["support"]["execution_platforms"]) for d in delivered
               if d["support"]["execution_platforms"] is not None and d["claim_status"] == "CLAIM_SUPPORTED"]
    model_summary = out.get("answer_summary", "")[:MAX_TEXT] if isinstance(out.get("answer_summary"), str) else ""
    sum_reasons = screen_free_text(model_summary, allowed_nums, allowed_ids, backing)
    stated, stated_removed = [], []
    for x in (out.get("not_established") or [])[:8]:
        if not isinstance(x, dict):
            continue
        t = f"{str(x.get('topic', ''))[:120]}: {str(x.get('statement', ''))[:400]}"
        r = screen_free_text(t, allowed_nums | numbers2(offer["question"]), allowed_ids, backing)
        (stated_removed if r else stated).append({"text": t, "reasons": r} if r else t)
    by_class = {c: sum(1 for d in delivered if d["evidence_class"] == c and d["class_confirmed"])
                for c in EVIDENCE_CLASSES}
    gaps = [c for c, n in by_class.items() if n == 0]
    n_sup = sum(1 for d in delivered if d["claim_status"] == "CLAIM_SUPPORTED")
    n_wh = sum(1 for r in removed if r["decision"] == "WITHHELD")
    n_rej = len(removed) - n_wh
    summary = (f"[{execution_class}] {len(delivered)} finding(s) delivered; each quotation was found verbatim in the "
               f"named pinned document. {n_sup} document-supported claim(s) (rule {CLAIM_RULE_ID}) are listed under "
               f"findings; {len(delivered) - n_sup} quote-verified excerpt(s) whose claims are NOT confirmed are listed "
               f"separately under unconfirmed_findings. "
               f"{n_wh} withheld and {n_rej} rejected by the verifier. No finding is independently verified as true. "
               + (f"No verified finding in this answer for: {', '.join(gaps)}." if gaps else ""))
    ok_checks = all(c["ok"] for c in checks)
    status = "VERIFIED" if ok_checks and not removed else "PARTIAL" if ok_checks and delivered else "FAILED"
    rc_reasons = ([f"{len(removed)} finding(s) removed"] if removed else []) + (
        [f"{len(delivered) - n_sup} delivered claim(s) not confirmed"] if len(delivered) > n_sup else []) + (
        ["model summary withheld"] if sum_reasons else []) + (
        [f"{len(stated_removed)} not-established statement(s) withheld"] if stated_removed else [])
    research = "FAIL" if status == "FAILED" or not n_sup else ("PARTIAL" if rc_reasons else "PASS")
    deliverable = {
        "summary": summary,
        # R1e: document-supported claims and unconfirmed claims are delivered in SEPARATE lists
        "findings": [d for d in delivered if d["claim_status"] == "CLAIM_SUPPORTED"],
        "unconfirmed_findings": [d for d in delivered if d["claim_status"] != "CLAIM_SUPPORTED"],
        "model_summary_unverified": None if sum_reasons else model_summary,
        "model_summary_withheld": {"reasons": sum_reasons} if sum_reasons else None,
        "uncertainties": [TRUTH_NOTE, f"independent_evidence {INDEPENDENT_EVIDENCE_NOTE}"]
                         + [f"{c}: {CLASS_CEILINGS[c]}" for c in EVIDENCE_CLASSES if by_class[c]],
        "unanswered": [f"{c}: not established by this answer (no verified finding; the documents may still address "
                       f"it)." for c in gaps]
                      + [f"MODEL-STATED LIMITATION (not verified): {s}" for s in stated],
        "not_established_withheld": stated_removed,
        "confidence": None,
        "class_coverage": by_class,
        "removed_findings": removed,
    }
    verification = {
        "status": status, "checks": checks, "verified_findings": len(delivered), "rejected_findings": len(removed),
        "quotes_checked": len(delivered), "input_flags": raw.get("input_flags", []),
        "verifier_version": VERIFIER_VERSION, "claim_rule": CLAIM_RULE_ID,
        "research_completeness": research, "research_completeness_reasons": rc_reasons or ["none"],
        "method": "R1d.2 deterministic verifier: source membership, anchor resolution, verbatim quote match, bounded "
                  "same-passage context unit, claim screen, numeric check, contradiction scan, class cues, lexical "
                  "support over the evidence span; no language model; model output never trusted as-is",
        "rules": {"normalization": NORMALIZATION_RULE, "quote": QUOTE_RULE, "context": CONTEXT_RULE,
                  "screen": SCREEN_RULE, "numeric": NUMERIC_RULE, "meaning": MEANING_RULE, "transform": TRANSFORM_RULE, "class": CLASS_RULE, "claim": LEX_RULE,
                  "truth": TRUTH_NOTE, "independent_evidence": INDEPENDENT_EVIDENCE_NOTE},
        "counts": {"model_findings": len(findings), "delivered": len(delivered), "claim_supported": n_sup,
                   "claim_unconfirmed": len(delivered) - n_sup, "withheld": n_wh, "rejected": n_rej,
                   "truth_independently_verified": 0},
        "decisions": decisions,
        "cited_sources": {d["source_document"]: d["source_sha256"] for d in delivered},
        "collection_manifest_digest": docset.manifest_digest,
    }
    return deliverable, verification


def recheck_delivered(artifact, docset):
    """Independent re-check used by the receipt verifier: every delivered quotation occurs in the cited passage of the
    supplied collection, and the cited files' hashes match. Returns (ok, problems)."""
    problems = []
    if docset.manifest_digest != artifact["document_set"]["manifest_digest"]:
        problems.append("collection manifest digest differs from the artifact's")
    for f in artifact["report"]["findings"] + artifact["report"].get("unconfirmed_findings", []):
        e = f["evidence"][0]
        p = docset.passages.get(e["passage_id"])
        if p is None or norm(e["quote"]) not in p:
            problems.append(f"{f['finding_id']}: quotation not found in {e['passage_id']}")
        if docset.files.get(f["source_document"]) != f["source_sha256"]:
            problems.append(f"{f['finding_id']}: source hash differs")
        ctx = (f.get("support") or {}).get("evidence_span", {}).get("context")
        if ctx and (ctx["passage_id"] != e["passage_id"] or p is None or norm(ctx["text"]) not in p):
            problems.append(f"{f['finding_id']}: context unit not inside the cited passage")
    return not problems, problems
