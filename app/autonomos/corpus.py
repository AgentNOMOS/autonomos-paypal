"""Approved document sets. Research may only use these pinned, licence-clear documents: no web access.

A document set = a directory with a SOURCES.json (origin repository, commit, licence) and markdown files. Each file
is split into numbered passages; the manifest digest binds the exact bytes the offer was priced against."""
import json
import math
import os
import re

from .canon import digest, sha256_bytes

TOKEN = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
STOP = set("the a an and or of to in on for is are was were be by with as at it this that from not no what which "
           "who how does do did can could would should has have had its their there than then these those into "
           "about over under only also any all each per".split())


def norm_ws(s):
    return re.sub(r"\s+", " ", s).strip()


def tokens(s):
    return [t for t in TOKEN.findall(s.lower()) if t not in STOP and len(t) > 1]


class DocumentSet:
    def __init__(self, root, set_id):
        self.root, self.id = root, set_id
        self.sources = json.load(open(os.path.join(root, "SOURCES.json")))
        self.files, self.passages, self.raw_passages = {}, {}, {}
        for name in sorted(os.listdir(root)):
            if not name.endswith(".md"):
                continue
            raw = open(os.path.join(root, name), "rb").read()
            self.files[name] = sha256_bytes(raw)
            text = raw.decode("utf-8")
            for i, block in enumerate(b for b in re.split(r"\n\s*\n", text) if b.strip()):
                pid = f"{set_id}/{name}#p{i:03d}"
                self.passages[pid] = norm_ws(block)
                self.raw_passages[pid] = block          # R1d.2: line structure (table rows, list items) for context
        self.manifest = {"set_id": set_id, "sources": self.sources, "files": self.files,
                         "passage_count": len(self.passages)}
        self.manifest_digest = digest(self.manifest)
        self._index()

    def _index(self):
        self.tf = {pid: tokens(t) for pid, t in self.passages.items()}
        n = len(self.tf)
        df = {}
        for toks in self.tf.values():
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.avgdl = sum(len(v) for v in self.tf.values()) / max(n, 1)

    def search(self, query_terms, k=8):
        """BM25 over passages. Deterministic; the model only proposes the query terms."""
        q = [t for qt in query_terms for t in tokens(qt)]
        scores = []
        for pid, toks in self.tf.items():
            if len(toks) < 4:
                continue
            s, dl = 0.0, len(toks)
            for t in set(q):
                f = toks.count(t)
                if f:
                    s += self.idf.get(t, 0) * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * dl / self.avgdl))
            if s > 0:
                scores.append((s, pid))
        scores.sort(key=lambda x: (-x[0], x[1]))
        return [pid for _, pid in scores[:k]]

    def quote_in_passage(self, pid, quote):
        p = self.passages.get(pid)
        return bool(p) and len(norm_ws(quote)) >= 12 and norm_ws(quote) in p


def load_sets(corpus_root):
    out = {}
    for d in sorted(os.listdir(corpus_root)):
        if os.path.exists(os.path.join(corpus_root, d, "SOURCES.json")):
            out[d] = DocumentSet(os.path.join(corpus_root, d), d)
    return out
