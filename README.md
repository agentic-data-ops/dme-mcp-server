# DME MCP Server

MCP Server for DME storage O&M

Exposes 16 action modules (427 actions) from `dme-python-sdk` as MCP V1 tools

- **MCP V1** (`mcp>=1.28,<2`, FastMCP)
- **Endpoint**: a single root MCP Server at `<endpoint>/mcp/v1` (e.g. `http://127.0.0.1:8000/mcp/v1`) — all module tools are registered on this one server, no per-module mounts
- **Tool naming**: dot-separated `<topic>.<action_key>` (e.g. `san.lun_list`)
- **Annotation**: every tool carries `annotations.topic` (e.g. `san`), `annotations.subtopic` (e.g. `lun`) and `annotations.action` (e.g. `list`) — extension fields
- **Docstring parsing**: action function docstrings are parsed into the tool's `description` / `inputSchema` (parameter descriptions) / `outputSchema` (Returns fields); supports both the Chinese (default branch) and English (`main-en` branch) docstrings of `dme-python-sdk`
- **Blacklist**: modeled after `cli.py`; `~/.config/pydme/blacklist.json` takes precedence. Every blacklisted tool gets an extra optional `accept_risk` parameter (default from `--accept-risk` / `DME_ACCEPT_RISK`); calling it without `accept_risk=true` is rejected with a `RISK_BLOCKED` response telling the caller to set it explicitly

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

All module tools live on a single root MCP server. Point your MCP client at:

- `http://<host>:<port>/mcp/v1` — e.g. `http://127.0.0.1:8000/mcp/v1`

`tools/list` returns every action from all modules; each tool is named `<topic>.<action_key>`, so clients can group/filter by module on the `<topic>` prefix. Every tool also carries `annotations.topic` / `annotations.subtopic` / `annotations.action` (non-standard extension fields, for reference).

## Options

| Option | Environment variable | Description |
|--------|----------------------|-------------|
| `--endpoint/-e` | `DME_API_ENDPOINT` | DME API address |
| `--user/-u` | `DME_API_USERNAME` | Username |
| `--password/-p` | `DME_API_PASSWORD` | Password |
| `--token` | `DME_API_AUTH_TOKEN` | Auth token (optional; skips login when provided) |
| `--timeout` | — | Request timeout (default 90) |
| `--no-cache-auth-token` | — | Disable token caching |
| `--accept-risk` | `DME_ACCEPT_RISK` | Default `accept_risk=true` for blacklisted high-risk tools (clients can still override per call) |
| `--mcp-server` | `DME_MCP_SERVER` | Listen address `host:port` (default: `127.0.0.1:8000`) |
| `--mcp-transport` | — | `streamable-http` (default) / `stdio` |
