"""Compatibility import for the finite query comparison adapter."""
import sys
from repo_graph import analysis_queries

# Preserve evaluator rule-version patches against the canonical implementation.
sys.modules[__name__] = analysis_queries
