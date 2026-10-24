"""Start Stagecraft AI: `python main.py` -> http://127.0.0.1:8000  (set PORT to use another port)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `backend` imports from any working directory

import uvicorn  # noqa: E402

from backend.app import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Stagecraft AI running at http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port)
