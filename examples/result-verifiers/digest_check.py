"""Compare a provider's claimed SHA-256 against independently opened bytes."""
import hashlib
import re

from artifact_verification import Counterevidence, Inconclusive


def inspect(_name, data, candidate):
    expected = candidate.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
        raise Inconclusive("digest.expected_sha256_missing")
    actual = hashlib.sha256(data).hexdigest()
    if expected != actual:
        raise Counterevidence("digest.mismatch", expected_sha256=expected, observed_sha256=actual)
    return {"algorithm":"sha256", "sha256":actual, "bytes":len(data)}
