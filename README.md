# DME MCP Server

MCP Server for DME storage O&M

Exposes 16 action modules (427 actions) from `dme-python-sdk` as MCP V1 tools

- **MCP V1** (`mcp>=1.28,<2`, FastMCP)
- **Endpoints**: one shared endpoint, one path per module, format `<endpoint>/mcp/v1/<module>` (e.g. `http://127.0.0.1:8000/mcp/v1/san`)
- **Root MCP Server**: `/mcp/v1` exposes a `list_tools` tool that enumerates every module endpoint and its tools (see [Root MCP Server](#root-mcp-server))
- **Tool naming**: dot-separated `<topic>.<action_key>` (e.g. `san.lun.list`)
- **Annotation**: every tool carries `meta = {"topic", "subtopic", "action"}`
- **Docstring parsing**: action function docstrings are parsed into the tool's `description` / `inputSchema` (parameter descriptions) / `outputSchema` (Returns fields); supports both the Chinese (default branch) and English (`main-en` branch) docstrings of `dme-python-sdk`
- **Blacklist**: modeled after `cli.py`; `~/.config/pydme/blacklist.json` takes precedence, high-risk operations are rejected by default (`--accept-risk` / `DME_ACCEPT_RISK=true` to allow)

## Installation

```bash
# Install dependency (Chinese)
pip install git+https://github.com/agentic-data-ops/dme-python-sdk.git

# Or install dependency (English)
pip install git+https://github.com/agentic-data-ops/dme-python-sdk.git@main-en

# Install the MCP Server
pip install git+https://github.com/agentic-data-ops/dme-mcp-server.git
```

## Usage

```bash
# streamable-http (default); DME connection params match the pydme CLI.
# Listens on 127.0.0.1:8000 unless --mcp-server or DME_MCP_SERVER is set.
dme-mcp-server --mcp-server 0.0.0.0:8000 \
  --endpoint https://192.168.1.100:26335 --user admin --password xxx

# Or use environment variables (omitting --mcp-server uses the 127.0.0.1:8000 default)
export DME_API_ENDPOINT=https://192.168.1.100:26335
export DME_API_USERNAME=admin
export DME_API_PASSWORD=xxx
dme-mcp-server

# stdio mode (--mcp-server is just a startup switch)
dme-mcp-server --mcp-server x --mcp-transport stdio \
  --endpoint ... --user ... --password ...
```

## Connecting

Each module is an independent MCP server. Point your MCP client at one of these URLs:

- Per module: `http://<host>:<port>/mcp/v1/<module>` — e.g. `http://127.0.0.1:8000/mcp/v1/san`, `/mcp/v1/nas`, `/mcp/v1/storage`
- Root: `http://<host>:<port>/mcp/v1` — root server, exposes only the `list_tools` tool

### Root MCP Server

Call the `list_tools` tool on the root server to enumerate every module endpoint and its tools:

```
tools/call  {"name": "list_tools", "arguments": {}}
```

Response (JSON text):

```json
[{"mcp_server_path": "http://<host>:<port>/mcp/v1/<module>",
  "tools": [{"name": "<tool_name>", "description": "<tool_description>"}]}]
```

Use the returned `mcp_server_path` values as the client URL to connect to the corresponding module server.

## Options

| Option | Environment variable | Description |
|--------|----------------------|-------------|
| `--endpoint/-e` | `DME_API_ENDPOINT` | DME API address |
| `--user/-u` | `DME_API_USERNAME` | Username |
| `--password/-p` | `DME_API_PASSWORD` | Password |
| `--token` | `DME_API_AUTH_TOKEN` | Auth token (optional; skips login when provided) |
| `--timeout` | — | Request timeout (default 90) |
| `--no-cache-auth-token` | — | Disable token caching |
| `--accept-risk` | `DME_ACCEPT_RISK` | Allow blacklisted high-risk operations |
| `--mcp-server` | `DME_MCP_SERVER` | Listen address `host:port` (default: `127.0.0.1:8000`) |
| `--mcp-transport` | — | `streamable-http` (default) / `stdio` |
