"""Locate the shared package for legacy scripts launched with only agent/ on sys.path."""
import sys
from pathlib import Path

root = str(Path(__file__).resolve().parents[1])
if root not in sys.path:
    sys.path.append(root)
