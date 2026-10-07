"""Compatibility import for the single packaged structural engine."""
import sys
from repo_graph import analysis_native
sys.modules[__name__] = analysis_native
