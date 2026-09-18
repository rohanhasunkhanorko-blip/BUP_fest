"""Top-level entrypoint shim for Render.

When Render ignores the ``rootDir: gridwise`` setting (it does, in some
deploy modes), the build & start commands run from the repo root. This file
makes ``uvicorn main:app --app-dir .`` work in that case by re-exporting the
real FastAPI application object.

Locally you should still run from the ``gridwise/`` subdir:
    cd gridwise && uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
import sys

# Make the real package importable whether or not we're running from
# gridwise/. Put the gridwise/ dir on sys.path first so the relative import
# inside app.main (``. import db``) resolves correctly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_GRIDWISE = os.path.join(_HERE, "gridwise")
if _GRIDWISE not in sys.path:
    sys.path.insert(0, _GRIDWISE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Import the real FastAPI instance.
from app.main import app  # noqa: E402,F401  (re-export)
