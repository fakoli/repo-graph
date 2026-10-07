"""Repository diagrams and local search."""
import hashlib
from pathlib import Path

__version__ = "0.6.0"
LOADED_CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
