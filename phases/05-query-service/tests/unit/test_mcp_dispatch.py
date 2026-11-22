"""Proof tests for MCP dispatch failure modes (F1, F2, F4, F5 below).

In-process app, real gateway, real SDK server, real inner ASGI dispatch; only
the inner routes for F2/F5 are test routes. Failure modes:

F1 a path argument with `/` or `\\` selects another route: httpx's
   ASGITransport decodes %2F into scope["path"], so quoting is not enough.
F2 a 2xx inner response that is not JSON is echoed, and any inner body is read
   whole before the caps apply (memory).
F4 a call to an unknown, hidden or denied tool leaves no audit line.
F5 an exception in a tool's result shaping escapes as a protocol error.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys

import httpx
import pytest
from fastapi.responses import StreamingResponse

from query_service import settings
from query_service.mcp.registry import Route, ToolSpec
from query_service.mcp.tools import NoArgs


class _StubStaticFiles:  # the SPA build exists only in the image
    def __init__(self, *args, **kwargs):
        pass


def _main():
    if "query_service.main" in sys.modules:
        return sys.modules["query_service.main"]
    import fastapi.staticfiles as fs

    original, fs.StaticFiles = fs.StaticFiles, _StubStaticFiles
    try:
        return importlib.import_module("query_service.main")
    finally:
        fs.StaticFiles = original


qsm = _main()
CHUNK = 1 << 20
streamed = {"chunks": 0}


def _big(media_type):
    def route():
        def gen():
            for _ in range(64):
                streamed["chunks"] += 1
                yield b"x" * CHUNK
        return StreamingResponse(gen(), media_type=media_type)
    return route


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_MODE", "oidc")
    monkeypatch.setattr(settings, "OIDC_DEV", True)
    monkeypatch.setattr(qsm, "StaticFiles", _StubStaticFiles)
    application = qsm.create_app()
    application.add_api_route("/test/big-text", _big("text/plain"))
    application.add_api_route("/test/big-json", _big("application/json"))
    seen = []

    async def spy(scope, receive, send):
        if scope["type"] == "http":
            seen.append(scope["path"])
        await application(scope, receive, send)

    registry = application.state.mcp.registry
    registry.app = spy
    for name, path in (("big_text", "/test/big-text"), ("big_json", "/test/big-json")):
        registry.tools[name] = ToolSpec(name, name, NoArgs, lambda a, p=path: Route("GET", p))
    registry.tools["bad_shape"] = ToolSpec("bad_shape", "x", NoArgs,
                                           lambda a: Route("GET", "/api/v1/edition", shape=lambda b: 1 / 0))
    streamed["chunks"] = 0
    return application, seen


def call(application, name, args=None):
    async def go():
        async with application.router.lifespan_context(application):
            transport = httpx.ASGITransport(app=application, client=("203.0.113.9", 1))
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
                token = (await c.get("/auth/dev/token", params={"roles": "viewer"})).json()["access_token"]
                r = await c.post("/mcp", headers={"authorization": f"Bearer {token}", "accept": "application/json",
                                                  "content-type": "application/json", "mcp-protocol-version": "2025-06-18"},
                                 json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": name, "arguments": args or {}}})
                return r.json()

    return asyncio.run(go())


def tool_error(reply):
    result = reply.get("result")
    assert result is not None and result.get("isError") is True, reply
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("name,args", [
    ("robot_summary", {"robot_id": "r1/data/imu"}),
    ("robot_topic_stats", {"robot_id": "r1/data", "message_type": "imu"}),
    ("robot_topic_stats", {"robot_id": "r1", "message_type": "imu\\..\\x"}),
    ("upload_detail", {"upload_id": "../robots/r1/data/x"}),
    ("upload_files", {"upload_id": "a\\b"}),
])
def test_f1_slash_in_a_path_argument_is_refused_before_dispatch(app, name, args):
    application, seen = app
    assert tool_error(call(application, name, args))["error"] == "invalid_arguments"
    assert seen == []


def test_f2_non_json_2xx_is_refused_unread(app):
    application, _ = app
    reply = call(application, "big_text")
    assert tool_error(reply)["error"] == "upstream_error"
    assert "xxxx" not in json.dumps(reply)
    assert streamed["chunks"] <= 8  # what the app's middleware layers hold in flight; reading the whole body would take all 64


def test_f2_large_json_body_is_cut_off(app):
    application, _ = app
    assert tool_error(call(application, "big_json"))["error"] == "response_too_large"
    # The cut-off limit plus what the app's middleware layers hold in flight; reading the whole body would take all 64.
    assert streamed["chunks"] <= 4 * settings.MCP_MAX_RESPONSE_BYTES // CHUNK + 8


def test_f4_unknown_tool_is_audited_without_the_callers_name_as_label(app, capsys):
    application, _ = app
    assert tool_error(call(application, "no_such_tool_" + "z" * 300))["error"] == "unknown_tool"
    audits = [json.loads(line)["audit"] for line in capsys.readouterr().out.splitlines() if line.startswith('{"audit"')]
    entry = [a for a in audits if a["action"] == "mcp.tool_call"][-1]
    assert entry["extra"]["tool"] == "unknown" and entry["extra"]["outcome"] == "unknown_tool"
    assert len(entry["extra"]["requested"]) <= 64


def test_f5_shape_failure_is_a_tool_error(app, capsys):
    application, _ = app
    assert tool_error(call(application, "bad_shape"))["error"] == "upstream_error"
    assert '"outcome": "upstream_error"' in capsys.readouterr().out
