#!/usr/bin/env python3
"""Compile the web UI (web/app.py) into a static site in dist/.

Every page and JSON endpoint is requested through Flask's test client and written to disk:
  /                        -> dist/index.html
  /mp/26/                  -> dist/mp/26/index.html
  /api/mp/26/speeches.json -> dist/api/mp/26/speeches.json
  /static/…                -> copied as-is
The result needs no server — host it on Cloudflare Pages, GitHub Pages, Netlify, S3, …

Usage:
  .venv/bin/python export_site.py                    # site served from the domain root
  .venv/bin/python export_site.py --base /mptracker  # served from a sub-path (e.g. GitHub Pages project site)
  .venv/bin/python export_site.py --out public

Preview:  python3 -m http.server -d dist 8000
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "web"))
import app as webapp  # noqa: E402


def urls(db_path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    people = [r[0] for r in conn.execute("SELECT DISTINCT person_id FROM speeches WHERE person_id IS NOT NULL ORDER BY 1")]
    parties = [r[0] for r in conn.execute("SELECT DISTINCT party_id FROM speeches WHERE party_id IS NOT NULL ORDER BY 1")]
    conn.close()
    out = ["/", "/parties/"]
    for pid in people:
        out += [f"/mp/{pid}/", f"/api/mp/{pid}/speeches.json"]
        out += [f"/api/mp/{pid}/cloud-{m}.json" for m in webapp.CLOUD_MODES]
    for party_id in parties:
        out.append(f"/party/{party_id}/")
        out += [f"/api/party/{party_id}/cloud-{m}.json" for m in webapp.CLOUD_MODES]
    return out


def target(out: Path, url: str) -> Path:
    rel = url.lstrip("/")
    return out / rel / "index.html" if url.endswith("/") else out / rel


NOT_FOUND = """<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>ไม่พบหน้า — MP Tracker</title>
<link rel="stylesheet" href="{base}/static/style.css"></head>
<body><main class="wrap"><h1>ไม่พบหน้านี้</h1><p><a href="{base}/">← กลับหน้าแรก</a></p></main></body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "dist")
    ap.add_argument("--base", default="", help="URL path prefix the site will be served under, e.g. /mptracker")
    args = ap.parse_args()
    base = "/" + args.base.strip("/") if args.base.strip("/") else ""

    if not webapp.DB_PATH.exists():
        sys.exit(f"{webapp.DB_PATH} not found — run build_db.py first.")
    t0 = time.time()
    webapp.app.config["WORD_STATS"] = webapp.load_word_stats()
    client = webapp.app.test_client()

    out: Path = args.out
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(ROOT / "web" / "static", out / "static")

    todo = urls(webapp.DB_PATH)
    total_bytes = 0
    for i, url in enumerate(todo, 1):
        resp = client.get(url, base_url=f"http://localhost{base}/")
        if resp.status_code != 200:
            sys.exit(f"{url} -> HTTP {resp.status_code}")
        path = target(out, url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.data)
        total_bytes += len(resp.data)
        if i % 500 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)} files", flush=True)

    (out / "404.html").write_text(NOT_FOUND.format(base=base), encoding="utf-8")
    print(f"Wrote {len(todo)} pages/data files ({total_bytes / 1e6:.0f} MB) to {out} in {time.time() - t0:.0f}s"
          + (f" for base path {base}/" if base else ""))


if __name__ == "__main__":
    main()
