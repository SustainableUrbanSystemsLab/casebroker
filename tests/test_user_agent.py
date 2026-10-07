"""Every urllib client of the broker names itself.

A proxy in front of a broker may refuse urllib's default `Python-urllib/3.x`
User-Agent outright, so a request that leaves it in place can fail before the
broker sees it. The node worker uses httpx and is not affected.
"""
from __future__ import annotations

import importlib.util
import pathlib
import urllib.request

import pytest

from casebroker import USER_AGENT, cli, repro

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


@pytest.fixture
def sent(monkeypatch):
    """The requests that would have left, through urlopen or an opener."""
    out: list[urllib.request.Request] = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: out.append(req) or Response())
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda self, req, data=None, timeout=None: out.append(req) or Response())
    return out


def agent(req: urllib.request.Request) -> str | None:
    return req.get_header("User-agent")


def test_the_agent_is_not_urllibs_own():
    assert USER_AGENT.startswith("casebroker/") and "urllib" not in USER_AGENT.lower()


def test_the_cli_names_itself_on_a_plain_get_and_through_an_opener(sent):
    cli._get("https://b", "/v1/status", "t")
    cli._call(urllib.request.build_opener(), "https://b", "GET", "/v1/releases")
    assert [agent(r) for r in sent] == [USER_AGENT, USER_AGENT]


def test_repro_names_itself(sent):
    repro._http("https://b/v1/cases/x", "t")
    assert agent(sent[0]) == USER_AGENT


@pytest.mark.parametrize("script", ["admit_thermal", "report_receipts"])
def test_the_scripts_name_themselves(sent, script):
    load(script).Broker("https://b", "t").call("GET", "/v1/status")
    ua = agent(sent[0])
    assert ua and ua.startswith("casebroker") and "urllib" not in ua.lower()
