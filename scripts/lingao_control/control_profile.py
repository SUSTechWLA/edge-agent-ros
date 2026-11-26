"""The field package carries the same Lingao control contract as the motion provider."""
import hashlib
import json
from pathlib import Path


# This field release is commissioned for exactly these endpoint and safety semantics.
# Inspector and brake can be copied together to a Jetson without the repository.
COMMISSIONED_PROFILE = "aea5049dc75e34a6acc03634016a89a01821bf3d4b683085afd1efd790386836"
PROFILE = Path(__file__).with_name("lingao-chassis-control.json")


def read_profile(path=PROFILE):
    raw = path.read_bytes()
    profile = json.loads(raw)
    canonical = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != COMMISSIONED_PROFILE:
        raise ValueError("Lingao chassis profile differs from the commissioned field release")
    return profile, hashlib.sha256(raw).hexdigest()
