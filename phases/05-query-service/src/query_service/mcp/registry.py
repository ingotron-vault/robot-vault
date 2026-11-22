"""MCP tools as thin wrappers over REST routes.

A tool is a `ToolSpec`: an argument model and a function that turns valid
arguments into one `Route`. Every call is one in-process request to that
route through the whole app (middleware, rate limiter, role checks, SQL
guard, limits, audit), with the caller's own `Authorization` and peer. MCP
therefore adds no data path and no privilege: what a token cannot do over
REST, it cannot do here.

Pro adds its tools through the same registry (`register_mcp(registry)` in the
Pro query component).
"""
from __future__ import annotations

import contextvars
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple, Type
from urllib.parse import quote

import httpx
import mcp_types as types
from pydantic import BaseModel, ValidationError

from .. import auth as auth_mod
from .. import middleware as middleware_mod
from .. import settings

logger = logging.getLogger(__name__)

if isinstance(middleware_mod.REQUESTS_TOTAL, middleware_mod._NoopMetric):
    TOOL_CALLS_TOTAL = middleware_mod._NoopMetric()
else:
    from prometheus_client import Counter

    TOOL_CALLS_TOTAL = Counter(
        "query_service_mcp_tool_calls_total",
        "MCP tool calls (via=mcp); the inner REST call is also counted as REST",
        ["tool", "outcome"],
    )


class Route(NamedTuple):
    method: str
    path: str
    params: Optional[Dict[str, Any]] = None
    body: Any = None
    # Applied to a 2xx JSON body: slicing, a hint. Never to an error body.
    shape: Optional[Callable[[Any], Any]] = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: Type[BaseModel]
    route: Callable[[Any], Route]
    write: bool = False
    idempotent: bool = True


class Caller(NamedTuple):
    """Who sent the outer /mcp request. Set by the gateway, never by a header."""

    request: Any  # starlette Request of the /mcp call (audit, cached user)
    authorization: Optional[str]
    peer: Tuple[str, int]


CALLER: contextvars.ContextVar[Caller] = contextvars.ContextVar("rv_mcp_caller")


def segment(value: str) -> str:
    """One path segment. A separator is refused, not quoted: the inner ASGI
    scope["path"] is percent-decoded, so %2F would still select another route."""
    if value in ("", ".", "..") or "/" in value or "\\" in value:
        raise ValueError("invalid path argument")
    return quote(value, safe="")


def topic_path(value: str) -> str:
    """A `{message_type:path}` value: slashes stay, dot segments do not."""
    parts = value.split("/")
    for part in parts:
        segment(part)
    return "/".join(quote(part, safe="") for part in parts)


# Bounded error codes. `detail` is the route's own short,
# already-public detail (what REST returns to the same caller), never a trace.
def _code_for(status: int, detail: Any) -> str:
    code = detail.get("code") if isinstance(detail, dict) else None
    text = detail if isinstance(detail, str) else ""
    if status == 422 and code == "result_limit_exceeded":
        return "too_many_rows"
    if status == 422 and isinstance(code, str) and (code.startswith("source_") or code.startswith("metadata_")):
        return "source_limit"
    if status in (400, 422) and (text.startswith("sql rejected") or text in ("invalid_sql", "query_invalid")):
        return "sql_rejected"
    if status == 504:
        return "timeout"
    if status == 503 and code == "query_capacity_exceeded":
        return "busy"
    return {
        400: "invalid_arguments",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        410: "not_found",
        413: "response_too_large",
        422: "invalid_arguments",
        429: "rate_limited",
    }.get(status, "upstream_error")


def _safe_detail(detail: Any) -> Any:
    if isinstance(detail, str) and len(detail) <= 200:
        return detail
    if isinstance(detail, dict) and all(isinstance(v, (str, int, float, bool)) for v in detail.values()):
        return detail
    return None  # FastAPI validation lists echo input; drop them.


def tool_error(code: str, detail: Any = None) -> types.CallToolResult:
    body = {"error": code}
    if detail is not None:
        body["detail"] = detail
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(body, separators=(",", ":")))],
        is_error=True,
    )


class _Refused(Exception):
    pass


def _bounded(app, limit: int, refused: list):
    """Wrap the inner app: a 2xx that is not JSON, or a body over `limit`, is
    stopped while it is sent, so it is never echoed or held whole in memory."""

    async def guarded_app(scope, receive, send):
        size = 0

        async def guarded_send(message):
            nonlocal size
            if message["type"] == "http.response.start":
                ctype = dict(message.get("headers") or []).get(b"content-type", b"")
                if 200 <= message["status"] < 300 and not ctype.startswith(b"application/json"):
                    refused.append("upstream_error")
                    raise _Refused
            elif message["type"] == "http.response.body":
                size += len(message.get("body", b""))
                if size > limit:
                    refused.append("response_too_large")
                    raise _Refused
            await send(message)

        await app(scope, receive, guarded_send)

    return guarded_app


def _oversized_field(value: Any, limit: int) -> bool:
    if isinstance(value, str):
        return len(value.encode("utf-8")) > limit
    if isinstance(value, dict):
        return any(_oversized_field(v, limit) for v in value.values())
    if isinstance(value, list):
        return any(_oversized_field(v, limit) for v in value)
    return False


def _success(body: Any) -> types.CallToolResult:
    if _oversized_field(body, settings.MCP_MAX_FIELD_BYTES):
        return tool_error("field_too_large", "a value is larger than the MCP field cap; select fewer columns or narrow the query")
    text = json.dumps(body, separators=(",", ":"), default=str)
    if len(text.encode("utf-8")) > settings.MCP_MAX_RESPONSE_BYTES:
        return tool_error("response_too_large", "the result is larger than the MCP response cap; narrow the query or lower the limit")
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


class ToolRegistry:
    """The tools this process serves. Switches apply at registration, so a tool
    that is off is absent from tools/list and unknown on call."""

    def __init__(self, app) -> None:
        self.app = app  # the full FastAPI app: inner calls cross its whole stack
        self.tools: Dict[str, ToolSpec] = {}

    def add(self, spec: ToolSpec) -> None:
        if spec.name in self.tools:
            raise ValueError(f"duplicate MCP tool {spec.name}")
        if spec.name in settings.MCP_TOOLS_DENY:
            return
        if spec.write and not settings.MCP_WRITE_TOOLS:
            return
        if spec.name == "query_sql" and not settings.MCP_SQL_ENABLED:
            return
        self.tools[spec.name] = spec

    def list_tools(self) -> list:
        return [
            types.Tool(
                name=s.name,
                description=s.description,
                input_schema=s.args.model_json_schema(),
                annotations=types.ToolAnnotations(
                    read_only_hint=not s.write,
                    destructive_hint=False,
                    idempotent_hint=s.idempotent,
                    open_world_hint=False,
                ),
            )
            for s in self.tools.values()
        ]

    async def call(self, name: str, arguments: Optional[Dict[str, Any]]) -> types.CallToolResult:
        spec = self.tools.get(name)
        if spec is None:  # also hidden and denied tools; never a metric label
            return self._done("unknown", tool_error("unknown_tool", "no such tool on this server"), None,
                              requested=str(name)[:64])
        try:
            route = spec.route(spec.args.model_validate(arguments or {}))
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) or "arguments" for e in exc.errors()})
            return self._done(name, tool_error("invalid_arguments", {"fields": ",".join(fields)}), None)
        except ValueError as exc:
            return self._done(name, tool_error("invalid_arguments", str(exc)), None)
        return await self._dispatch(name, route)

    async def _dispatch(self, name: str, route: Route) -> types.CallToolResult:
        caller = CALLER.get()
        correlation_id = uuid.uuid4().hex
        headers = {"x-correlation-id": correlation_id}
        if caller.authorization:
            headers["authorization"] = caller.authorization
        params = {k: v for k, v in (route.params or {}).items() if v is not None}
        # A fresh client per call: nothing (headers, cookies) is shared between
        # callers. `client=` carries the original peer, so the REST rate limiter
        # and audit see the same address a REST call from that peer would.
        refused: list = []
        app = _bounded(self.app, 4 * settings.MCP_MAX_RESPONSE_BYTES, refused)
        transport = httpx.ASGITransport(app=app, client=caller.peer, raise_app_exceptions=False)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://query.mcp.internal") as client:
                response = await client.request(
                    route.method, route.path, params=params, headers=headers,
                    json=route.body,
                )
        except Exception as exc:
            logger.warning("MCP tool %s dispatch failed (%s)", name, type(exc).__name__)
            return self._done(name, tool_error("upstream_error"), None, correlation_id)
        if refused:
            return self._done(name, tool_error(refused[0]), None, correlation_id)
        try:
            body = response.json()
        except ValueError:
            body = None if response.status_code < 300 else response.text[:200]
        if 200 <= response.status_code < 300:
            try:
                result = _success(route.shape(body) if route.shape else body) if body is not None else tool_error("upstream_error")
            except Exception as exc:
                logger.warning("MCP tool %s result shaping failed (%s)", name, type(exc).__name__)
                result = tool_error("upstream_error")
        else:
            detail = body.get("detail") if isinstance(body, dict) else body
            result = tool_error(_code_for(response.status_code, detail), _safe_detail(detail))
        return self._done(name, result, response.status_code, correlation_id)

    def _done(self, name, result, status, correlation_id=None, requested=None) -> types.CallToolResult:
        outcome = json.loads(result.content[0].text).get("error", "ok") if result.is_error else "ok"
        TOOL_CALLS_TOTAL.labels(name, outcome).inc()
        caller = CALLER.get(None)
        if caller is not None:
            auth_mod.audit(caller.request, "mcp.tool_call", {
                "via": "mcp", "tool": name, "status": status,
                "outcome": outcome, "correlation_id": correlation_id,
                **({"requested": requested} if requested is not None else {}),
            })
        return result
