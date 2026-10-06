#!/usr/bin/env python3
"""Run the installed skill without changing the caller's working directory."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repo_graph.cli import main
raise SystemExit(main())
