#!/usr/bin/env python3
"""Compatibility entrypoint for the original repository mapper."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repo_graph.cli import main
raise SystemExit(main(['map', *sys.argv[1:]]))
