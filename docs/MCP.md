# MCP server: let AI agents read Robot Vault

Robot Vault has a [Model Context Protocol](https://modelcontextprotocol.io/)
(MCP) server. An AI agent, for example Claude Code, Codex or Cursor, can use it
to list robots and uploads, read schemas, and run read-only SQL over your
recordings.

Readers: operators who connect an agent, and developers who change the server.
Edition: core and Pro. Pro adds tools; they are documented with the Pro edition.

## What it is

- One endpoint: `POST /mcp` on the query service (the same host and port as
  the API and the UI, for example `https://vault.example.com/mcp`).
- Transport: MCP Streamable HTTP, stateless, JSON responses. `GET` and
  `DELETE` on `/mcp` return 405.
- Protocol revisions: 2026-07-28 (per-request envelope) and the initialize
  handshake revisions 2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25.
- Library: the official `mcp` Python SDK 2.3.0 (MIT licence).
- Every tool calls one existing REST route inside the service, with **your**
  token. So every role check, SQL guard, query limit, rate limit and audit
  record is the API's own. MCP adds no new data access and no new privilege.

```text
agent --HTTPS POST /mcp, Bearer token--> query service
          gateway: ingress rate limit -> Host/Origin -> role check (viewer)
          MCP SDK: tools/list, tools/call
          tool --in-process REST call, same token and peer--> /api/v1/...
```

## Before you start

- A running Robot Vault with `AUTH_MODE=oidc` (every release). With
  `AUTH_MODE=none`, MCP is off unless you set `MCP_ALLOW_NO_AUTH=true`
  (development only; see [Configuration](#configuration)).
- `PUBLIC_BASE_URL` set to the URL the agent uses, for example
  `https://vault.example.com`. The server refuses other `Host` values (421),
  except `localhost`, `127.0.0.1` and `[::1]`.
- An access token from your identity provider (IdP) for a user with the
  `viewer`, `operator` or `admin` role. The token audience must be the
  API's `OIDC_AUDIENCE`. MCP uses the same tokens as the API and the UI.

### Get a token

Version 1 supports **static bearer tokens** only. Get an access token from
your IdP (for example with your IdP's CLI or a client-credentials grant) and
put it in an environment variable:

```bash
export ROBOT_VAULT_TOKEN='<access token>'
```

Tokens expire (`OIDC_MAX_TOKEN_LIFETIME_SECONDS`, at most 24 hours). When the
agent gets HTTP 401, get a new token and restart the agent's MCP connection.

In a development stack with `OIDC_DEV=true` only:

```bash
export ROBOT_VAULT_TOKEN=$(curl -s 'http://localhost:8003/auth/dev/token?roles=viewer' | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
```

Interactive OAuth login from the agent (dynamic client registration, PKCE)
is not supported in version 1. The server publishes OAuth protected-resource
metadata (RFC 9728) at `/.well-known/oauth-protected-resource` when a real
issuer is configured, so clients can find the issuer, but whether a client
can log in depends on your IdP. This is future work.

## Connect an agent

Replace `https://vault.example.com` with your `PUBLIC_BASE_URL`.

**Claude Code:**

```bash
claude mcp add --transport http robot-vault https://vault.example.com/mcp \
  --header "Authorization: Bearer $ROBOT_VAULT_TOKEN"
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.robot-vault]
url = "https://vault.example.com/mcp"
bearer_token_env_var = "ROBOT_VAULT_TOKEN"
```

**Cursor** (`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "robot-vault": {
      "url": "https://vault.example.com/mcp",
      "headers": { "Authorization": "Bearer ${env:ROBOT_VAULT_TOKEN}" }
    }
  }
}
```

**Python, with the official SDK** (`pip install mcp`):

```python
import asyncio, os
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

async def main():
    headers = {"Authorization": f"Bearer {os.environ['ROBOT_VAULT_TOKEN']}"}
    async with httpx2.AsyncClient(headers=headers) as http:
        async with Client(streamable_http_client("https://vault.example.com/mcp", http_client=http)) as client:
            print([t.name for t in (await client.list_tools()).tools])
            result = await client.call_tool("query_sql", {"sql": "SELECT count(*) FROM all_parquet"})
            print(result.content[0].text)

asyncio.run(main())
```

What was tested: the SDK client above, against a real stack, with every
protocol revision named here (see [Tests](#tests)). The Claude Code, Codex
and Cursor settings follow those tools' documented formats; they were not
run in the test suite. If a client cannot connect, check its own MCP
documentation for HTTP transport and headers.

Expected result: the agent lists the tools below. Ask it, for example,
"Which robots uploaded data this week, and how many rows per topic?"

## Tools (core)

All core tools are read-only. Each one calls the REST route in the last
column with your token; the route decides what your role may do.

| Tool | What it returns | REST route |
| --- | --- | --- |
| `get_edition` | Edition, versions, MCP switches. In core, also what Pro adds and where to get it. | `GET /api/v1/edition` |
| `list_robots` | Robots with upload counts and bytes. `limit` (max 200) and `offset` slice the full list on the server side of MCP; the route itself has no paging. | `GET /api/v1/robots/list` |
| `robot_summary` | One robot: totals, topics, recent uploads. | `GET /api/v1/robots/{robot_id}/summary` |
| `robot_time_range` | First and last message time of a robot. | `GET /api/v1/robots/{robot_id}/time-range` |
| `robot_topic_stats` | Row counts and bounds of one topic, optionally per bucket. | `GET /api/v1/robots/{robot_id}/stats/{message_type}` |
| `list_message_types` | Topics with published Parquet data. | `GET /api/v1/message-types` |
| `get_schema` | Columns, schema versions, file/row counts, time range. | `GET /api/v1/schema` |
| `query_sql` | Result of one read-only DuckDB `SELECT` over the view `all_parquet`. | `POST /api/v1/query/sql` |
| `explain_query` | Which Parquet files a filter selects, before reading data. | `GET /api/v1/query/plan` |
| `query_limits` | Source, time and concurrency limits for queries. | `GET /api/v1/query/limits` |
| `list_uploads` | Uploads, paged (`limit` max 200, `offset`). | `GET /api/v1/uploads/list` |
| `upload_detail` | One upload: status, integrity, topics, rows. | `GET /api/v1/uploads/{upload_id}/detail` |
| `upload_files` | Parquet files of one upload. | `GET /api/v1/uploads/{upload_id}/files` |
| `uploads_summary` | Upload counts and bytes per robot and window. | `GET /api/uploads/summary` |
| `metrics_overview` | The Overview page numbers. | `GET /api/v1/metrics/overview` |
| `timeseries` | Counts per time bucket for one topic. | `GET /api/timeseries` |
| `obs_health` | Whether Prometheus (the `obs` profile) answers. | `GET /api/obs/health` |
| `obs_alerts` | Alerts that Prometheus has firing. | `GET /api/obs/alerts` |

A successful result is the route's JSON, unchanged, as text. Tool
annotations: `readOnlyHint=true`, `destructiveHint=false`,
`openWorldHint=false`.

## Errors

- **Authentication** fails at the HTTP level, so the client can ask for a new
  token: 401 with `WWW-Authenticate: Bearer resource_metadata="<PUBLIC_BASE_URL>/.well-known/oauth-protected-resource"`
  (the `resource_metadata` part only when a real issuer is configured), or 403
  with `error="insufficient_scope"` for a token without a Robot Vault role.
- **Transport**: 429 (ingress limit), 421 (`Host` not allowed), 403
  (`Origin` not allowed), 400/406 (wrong `Content-Type`/`Accept`), 405 (`GET`, `DELETE`).
- **Tool failures** are tool results with `isError: true` and a JSON body
  `{"error": "<code>", "detail": ...}`. `detail` is the route's own short
  message, never a stack trace, token or internal URL.

| Code | Meaning | What the agent should do |
| --- | --- | --- |
| `sql_rejected` | The SQL guard refused the statement (not a read-only `SELECT`, a table function, a file path, a configuration function). | Rewrite as a `SELECT` over `all_parquet`. |
| `too_many_rows` | More than 10,000 result rows (the API's 422). | Aggregate, or add `LIMIT`. |
| `source_limit` | The query would read too many files, rows, bytes or too long a time window. | Narrow robot, topic or time. |
| `timeout` | The query passed `DUCKDB_QUERY_TIMEOUT_SECONDS`. | Narrow the query. |
| `busy` | All query slots are in use. | Retry later. |
| `rate_limited` | The REST operation limit for your address is used up. | Wait for the next minute. |
| `response_too_large`, `field_too_large` | The result is larger than `MCP_MAX_RESPONSE_BYTES`, or one value is larger than `MCP_MAX_FIELD_BYTES`. | Select fewer columns or rows. |
| `forbidden`, `unauthorized`, `not_found`, `invalid_arguments`, `unknown_tool`, `upstream_error` | As named. `unknown_tool` also means a tool that is switched off on this server. | |

## Safety

- **`query_sql` reads all recordings the token can see.** Read-only does not
  mean narrow: the view `all_parquet` covers every published recording. If an
  agent should not read everything, give it a token for a restricted user, or
  switch SQL off (`MCP_SQL_ENABLED=false`) or deny tools (`MCP_TOOLS_DENY`).
- **Returned data is untrusted text.** Robot names, labels, schemas and values
  come from recordings and users. A recording can contain text that tries to
  instruct the agent ("prompt injection"). The server returns it as data and
  it cannot change what the server does, but the agent may still act on it.
  Tool descriptions say this; the annotations do not prevent it. Use client
  settings that ask you before a write or an outbound action.
- **Write tools are off by default** (`MCP_WRITE_TOOLS=false`). Core has none.
  When on, a write tool is listed and callable, and its route still checks
  the role. When off, it is neither listed nor callable.
- **Rate limits.** `/mcp` has its own per-address ingress limit
  (`MCP_INGRESS_RATE` per minute). It applies before the token check, so it
  also limits floods of bad tokens. Each tool call also uses the normal REST
  operation limit once (`RATE_LIMIT_DEFAULT_PER_MINUTE`), exactly like the
  same REST call. The outer `/mcp` request does not use the REST limit.
- **Audit.** Each tool call writes one audit line with `"action":
  "mcp.tool_call"`, `"via": "mcp"`, the tool, the HTTP status of the inner
  route, an outcome code, a correlation id, and your user and address. A call
  to an unknown, hidden or denied tool is audited as tool `unknown`, with the
  requested name cut to 64 characters in `requested`. The
  REST routes write their own audit lines as before, with the same user and
  address. Prometheus counts calls in `query_service_mcp_tool_calls_total`
  and gateway outcomes in `query_service_mcp_requests_total`; the inner REST
  call is counted as REST too.
- **Memory.** A tool call holds the route's JSON body and the tool result
  in the query process at the same time, so a large `query_sql` or
  `list_robots` result uses about three times the memory of the same REST
  call. The inner body is cut off at 4 × `MCP_MAX_RESPONSE_BYTES`, and a
  successful route answer that is not JSON is refused (`upstream_error`)
  without being read.
- **Path arguments** (robot, upload, episode and dataset ids) may not contain
  `/` or `\`; such a value is `invalid_arguments`, so it cannot reach another
  route.
- **Browser access** is refused unless the page origin is in
  `MCP_ALLOWED_ORIGINS` (default: none). CLI and IDE clients send no `Origin`
  and are not affected.

## Configuration

Read when the query service starts; change them by recreating the service.
`AUTH_MODE` stays live, as for the API.

| Variable | Default | Effect |
| --- | --- | --- |
| `MCP_ENABLED` | `true` | `false` removes `/mcp` (404) and the metadata route. |
| `MCP_WRITE_TOOLS` | `false` | `true` lists and allows the Pro write tools. |
| `MCP_SQL_ENABLED` | `true` | `false` removes `query_sql`. |
| `MCP_TOOLS_DENY` | empty | Comma-separated tool names to remove. |
| `MCP_ALLOW_NO_AUTH` | `false` | With `AUTH_MODE=none`, `true` serves MCP without a token. Development only: the dev compose publishes the API on `127.0.0.1`. |
| `MCP_ALLOWED_ORIGINS` | empty | Comma-separated browser origins allowed to call `/mcp`. |
| `MCP_INGRESS_RATE` | `240` | `/mcp` requests per client address per minute. |
| `MCP_MAX_RESPONSE_BYTES` | `1048576` | Largest tool result (serialized JSON). |
| `MCP_MAX_FIELD_BYTES` | `65536` | Largest single string value in a result. |
| `PUBLIC_BASE_URL` | unset | `scheme://host[:port]` clients use. Allowed `Host`, the protected-resource id, and the base URL Pro tools use. |

`GET /api/v1/edition` reports `"mcp": {"enabled": ..., "write_tools": ...}`
for this service. `enabled` is `false` when `MCP_ENABLED=false`, or when
`AUTH_MODE=none` without `MCP_ALLOW_NO_AUTH=true`.

## Tests

- Unit tests: `phases/05-query-service/tests/unit/test_mcp_tools.py` (run by
  `make test-unit`).
- Before each release, maintainers run the SDK client against a real stack
  with every protocol revision named above.

## Developer notes

- Code: `phases/05-query-service/src/query_service/mcp/` (`__init__.py`
  gateway and mount, `registry.py` dispatch and errors, `tools.py` core tools).
- A tool is a `ToolSpec`: a Pydantic argument model and a function that
  returns one `Route`. Path arguments go through `segment()`, so an argument
  cannot select another route.
- Pro adds tools through the optional hook `register_mcp(registry)` of the Pro
  query component. `ADAPTER_API` is unchanged.
