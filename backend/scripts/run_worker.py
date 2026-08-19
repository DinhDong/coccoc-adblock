#!/usr/bin/env python
"""
Standalone entry-point for the background worker service.

Equivalent to the docker-compose command:
    python -m app.services.worker --source=db --sleep=5

Usage (from backend/):
    python scripts/run_worker.py
    python scripts/run_worker.py --source=files --sleep=10 --once

All CLI flags from app.services.worker are supported — this script
simply delegates to the worker's main() function.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the backend root is on sys.path so `app.*` imports resolve
# regardless of where the script is invoked from.
_backend_root = str(Path(__file__).resolve().parents[1])
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

from app.services.worker import main  # noqa: E402


if __name__ == "__main__":
    # Default args match what docker-compose.yml used to specify:
    #   --source=db --sleep=5
    # If the caller passes explicit CLI args, those take precedence.
    default_args = ["--source=db", "--sleep=5"]
    argv = sys.argv[1:] if len(sys.argv) > 1 else default_args
    sys.exit(main(argv))
