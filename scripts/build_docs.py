"""Build the self-contained submission files: report.html and slides.html (repo root).

    python scripts/build_docs.py                 # --repo URL to use a different GitHub link

Reads docs/src/report.html and docs/src/slides.html, embeds every screenshot (docs/screenshots/*.jpg) as a data URI so
the files open by double-click on any computer, and fills in the GitHub link. The slides load reveal.js from a CDN.
"""

from __future__ import annotations

import argparse
import base64
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "docs" / "src"
SHOTS = REPO_ROOT / "docs" / "screenshots"


def build(name: str, repo_url: str) -> Path:
    html = (SRC / name).read_text()

    def embed(m: re.Match) -> str:
        path = SHOTS / m.group(1)
        if not path.exists():
            raise SystemExit(f"{name}: missing screenshot {path.name}")
        return f'src="data:image/jpeg;base64,{base64.b64encode(path.read_bytes()).decode()}"'

    html = re.sub(r'src="screenshots/([^"]+)"', embed, html)
    html = html.replace("{{REPO_URL}}", repo_url)
    out = REPO_ROOT / name
    out.write_text(html)
    print(f"wrote {out.name} ({out.stat().st_size / 1e6:.1f} MB)")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="https://github.com/Kalab-yale/mgt409-final-stagecraft", help="public GitHub repo URL shown in the report and slides")
    args = ap.parse_args()
    for f in ("report.html", "slides.html"):
        build(f, args.repo)
