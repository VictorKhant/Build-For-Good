"""Serverless entry point.

Vercel runs the ASGI app it finds as `app` in this file. Everything real lives
in `bellyup/`, whose modules import each other flat (`import demo_data`), so
that directory goes on the path before the app is imported -- the same shape as
running `uvicorn app:app` from inside it.

Nothing here is used when running locally; `bellyup/app.py` remains the thing
you run in development.
"""

import sys
from pathlib import Path

BELLYUP = Path(__file__).resolve().parent.parent / "bellyup"
if str(BELLYUP) not in sys.path:
    sys.path.insert(0, str(BELLYUP))

from app import app  # noqa: E402  (path has to be set first)

__all__ = ["app"]
