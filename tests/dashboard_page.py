"""The dashboard as a browser assembles it: the shell, with the content-named
assets it links to put back where the links are.

The app serves dashboard.html split (app._DashboardBundle), so the text of
``GET /`` is no longer the whole page. A test asking "does the page the browser
gets carry X" has to fetch what the browser fetches.
"""
from __future__ import annotations

import re

_ASSET = re.compile(r'<link rel="stylesheet" href="(/assets/[^"]+)">|<script src="(/assets/[^"]+)"></script>')


def served_page(client, **kwargs) -> str:
    shell = client.get("/", **kwargs)
    assert shell.status_code == 200, shell.status_code

    def inline(m: re.Match) -> str:
        css, js = m.group(1), m.group(2)
        r = client.get(css or js, **kwargs)
        assert r.status_code == 200, (css or js, r.status_code)
        return f"<style>{r.text}</style>" if css else f"<script>{r.text}</script>"

    return _ASSET.sub(inline, shell.text)
