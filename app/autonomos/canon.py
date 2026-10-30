"""Canonical JSON and hashing. Every digest in AutoNOMOS is over canonical JSON (sorted keys, no whitespace)."""
import hashlib
import json


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))


def digest(obj) -> str:
    return sha256_text(canonical_json(obj))
