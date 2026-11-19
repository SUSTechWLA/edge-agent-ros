"""Legacy import path for the public v1 module event contract."""
import sys
from pathlib import Path

# Legacy file-based entry points put only this module's directory on sys.path.
if not __package__:
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.append(root)

from edge_contracts.events import ASSIGNMENT, BEARER, IDENTIFIER, SENSITIVE, identifier, parse_event, scrub
