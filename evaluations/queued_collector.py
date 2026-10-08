"""Compatibility import for the single packaged owned collector queue."""
import sys
from repo_graph import analysis_queue
sys.modules[__name__] = analysis_queue
