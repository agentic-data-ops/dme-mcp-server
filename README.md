# DME MCP Server

MCP Server for DME storage O&M

Exposes the `dme-python-sdk` action library (16 topics, 427 actions) through **4 meta tools**
instead of registering every action as its own MCP tool (which made the tool list too long
for LLM context):

- **MCP V1** (`mcp>=1.28,<2`, FastMCP)
- **Endpoint**: a single root MCP Server at `<endpoint>/mcp/v1` (e.g. `http://127.0.0.1:8000/mcp/v1`)
- **Meta tools**:
  | Tool | Parameters | Description |
  |------|-----------|-------------|
  | `list_topics` | — | All topics and subtopics with descriptions (same as `pydme --list-topics`) |
  | `list_actions` | `topic`, `subtopic` (optional) | Actions of a topic / one subtopic with descriptions; without `subtopic` lists the direct actions and every subtopic's actions (same as `pydme <topic> [<subtopic>] --help`) |
  | `show_action_help` | `topic`, `action`, `subtopic` (optional) | Description / parameters / output of one action (same as `pydme <topic> [<subtopic>] <action> --help`) |
  | `exec_action` | `topic`, `action`, `params` (dict), `subtopic` (optional), `accept_risk` (optional) | Execute one action with a parameter dict (same as `pydme <topic> [<subtopic>] <action> --param value ...`) |
- **Docstring parsing**: action help text is loaded from `dme-python-sdk`'s i18n resources
  (`pydme/config/i18n/actions_<lang>.yaml` + `funcs_<lang>.yaml`, language via `--lang` /
  `DME_CLI_LANG`, default zh_CN), falling back to the function docstring when a resource
  entry is missing; the action registry comes from `pydme.i18n.load_actions_registry`
- **Blacklist**: modeled after `cli.py`; `~/.config/pydme/blacklist.json` takes precedence.
  `exec_action` rejects blacklisted high-risk operations with a `RISK_BLOCKED` response
  unless `accept_risk=true` is passed (or the server was started with `--accept-risk` /
  `DME_ACCEPT_RISK`)

## Installation

`dme-python-sdk` is declared as a git dependency (the default `main` branch, which carries the
i18n action-help resources) and is installed automatically — no separate SDK install needed:

```bash
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

The 4 meta tools live on a single root MCP server. Point your MCP client at:

- `http://<host>:<port>/mcp/v1` — e.g. `http://127.0.0.1:8000/mcp/v1`

`tools/list` returns the 4 tools above. Typical flow: `list_topics` to discover the topic
tree → `list_actions` to pick an action → `show_action_help` for its parameters →
`exec_action` to run it.

## Options

| Option | Environment variable | Description |
|--------|----------------------|-------------|
| `--endpoint/-e` | `DME_API_ENDPOINT` | DME API address |
| `--user/-u` | `DME_API_USERNAME` | Username |
| `--password/-p` | `DME_API_PASSWORD` | Password |
| `--token` | `DME_API_AUTH_TOKEN` | Auth token (optional; skips login when provided) |
| `--timeout` | — | Request timeout (default 90) |
| `--no-cache-auth-token` | — | Disable token caching |
| `--accept-risk` | `DME_ACCEPT_RISK` | Default `accept_risk=true` for blacklisted high-risk `exec_action` calls (clients can still override per call) |
| `--lang` | `DME_CLI_LANG` | Action help language: `zh_CN` (default) / `en_US` (mirrors pydme CLI) |
| `--mcp-server` | `DME_MCP_SERVER` | Listen address `host:port` (default: `127.0.0.1:8000`) |
| `--mcp-transport` | — | `streamable-http` (default) / `stdio` |
