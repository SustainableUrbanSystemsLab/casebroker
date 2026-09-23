"""Serve a folder of <case_id>.wfld wind fields to the dashboard.

The broker holds pointers, not bytes (its database is ~500 MB; a campaign of
fields is tens of GB), so the dashboard's viewer fetches <source>/<case_id>.wfld
from wherever the fields are. Until there is a bucket, that is the master, where
the archives already land -- and this is the server for it: read-only, *.wfld
only, from one folder.

Two headers are the whole reason this is not `python -m http.server`: the
dashboard is served from the broker's origin (https on Render), so the browser
refuses a cross-origin read without Access-Control-Allow-Origin, and Chrome
additionally refuses a public page reading a private address (localhost, a LAN
IP) unless the preflight answers Access-Control-Allow-Private-Network.

  uv run python scripts/serve_fields.py E:/wind/fields --port 8765
  # then, in the dashboard: Settings -> Preferences -> Wind-field source:
  #   http://localhost:8765

Binds to 127.0.0.1 by default: the fields are the campaign's results, and this
has no authentication. --host 0.0.0.0 to share on a trusted network.
"""

from __future__ import annotations

import argparse
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9._-]*)\.wfld$")


def handler_for(root: Path, origin: str):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            # A finished case's field never changes; a backfill writes a new file.
            self.send_header("Cache-Control", "public, max-age=3600")
            super().end_headers()

        def do_OPTIONS(self):
            self.send_response(204)
            self.end_headers()

        def _allowed(self) -> bool:
            path = self.path.split("?", 1)[0]
            m = NAME.match(path)
            if not m or not (root / f"{m.group(1)}.wfld").is_file():
                self.send_error(404, "no such field")
                return False
            return True

        def do_GET(self):
            if self._allowed():
                super().do_GET()

        def do_HEAD(self):
            if self._allowed():
                super().do_HEAD()

        def guess_type(self, path):
            return "application/octet-stream"

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", type=Path, help="holds <case_id>.wfld files")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--origin", default="*",
                    help="the dashboard's origin to allow (default any: these are read-only files)")
    a = ap.parse_args()
    root = a.folder.resolve()
    if not root.is_dir():
        raise SystemExit(f"{root}: not a folder")
    n = len(list(root.glob("*.wfld")))
    print(f"serving {n} wind field(s) from {root} on http://{a.host}:{a.port}/<case_id>.wfld")
    ThreadingHTTPServer((a.host, a.port), handler_for(root, a.origin)).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
