#!/usr/bin/env python
"""DME MCP Server (MCP V1).

Exposes a fixed set of 4 meta tools over the dme-python-sdk action library
(instead of registering every action as its own MCP tool, which made the tool
list too long for LLM context):

  list_topics       -- all topics and subtopics with descriptions
                       (same as ``pydme --list-topics``)
  list_actions      -- actions of a topic / one subtopic with descriptions
                       (same as ``pydme <topic> [<subtopic>] --help``)
  show_action_help  -- description / parameters / output of one action
                       (same as ``pydme <topic> [<subtopic>] <action> --help``)
  exec_action       -- execute one action with a parameter dict
                       (same as ``pydme <topic> [<subtopic>] <action> --param value ...``)

- Help text comes from dme-python-sdk i18n resources
  (``pydme/config/i18n/actions_<lang>.yaml`` + ``funcs_<lang>.yaml``, selected by
  ``--lang`` / ``DME_CLI_LANG``, default zh_CN), falling back to the function
  docstring when a resource entry is missing
- The action registry comes from ``pydme.i18n.load_actions_registry`` (func
  names resolved to callables, mirroring the new cli.py)
- Blacklist: uses ``pydme.cli.load_blacklist`` (``~/.config/pydme/blacklist.json`` takes
  precedence over the SDK default, same policy as the pydme CLI); blacklisted high-risk
  operations are rejected by default (``--accept-risk`` / ``DME_ACCEPT_RISK`` or the
  per-call ``accept_risk=true`` argument to allow)
"""

import argparse
import inspect
import os
import sys
from typing import Annotated, Any, Dict, List, Optional

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from pydme.client import DMEAPIClient
from pydme.cli import DMECLI, load_blacklist
from pydme.i18n import get_action_doc, load_actions, load_actions_registry, resolve_lang


# ---------------------------------------------------------------------------
# Argument parsing (DME connection params mirror pydme/cli.py)
# ---------------------------------------------------------------------------

def create_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        prog='dme-mcp-server',
        description='DME MCP Server - exposes dme-python-sdk actions via 4 meta tools',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''\
Examples:
  dme-mcp-server --mcp-server 0.0.0.0:8000 \\
      --endpoint https://192.168.1.100:26335 --user admin --password xxx
  # Environment variables: DME_MCP_SERVER / DME_API_ENDPOINT / DME_API_USERNAME / DME_API_PASSWORD
  # Endpoint: http://<host>:<port>/mcp/v1
  # Tools: list_topics / list_actions / show_action_help / exec_action''',
    )

    # DME connection params (mirror cli.py; environment variables supported)
    parser.add_argument('--endpoint', '-e', default=os.environ.get('DME_API_ENDPOINT'),
                        help='DME API endpoint address, format: https://<ip>:<port>')
    parser.add_argument('--user', '-u', default=os.environ.get('DME_API_USERNAME'),
                        help='DME API username')
    parser.add_argument('--password', '-p', default=os.environ.get('DME_API_PASSWORD'),
                        help='DME API password')
    parser.add_argument('--token', default=os.environ.get('DME_API_AUTH_TOKEN'),
                        help='DME API auth token (optional; skips login when provided)')
    parser.add_argument('--timeout', type=int, default=90,
                        help='API request timeout in seconds, default 90')
    parser.add_argument('--no-cache-auth-token', action='store_false', dest='cache_auth_token',
                        default=True, help='disable auth token caching (caching enabled by default)')

    # MCP Server params
    parser.add_argument('--mcp-server', default=os.environ.get('DME_MCP_SERVER') or '127.0.0.1:8000',
                        help='MCP Server listen address host:port (default: 127.0.0.1:8000); '
                             'can also be set via the DME_MCP_SERVER environment variable')
    parser.add_argument('--mcp-transport', choices=['streamable-http', 'stdio'],
                        default='streamable-http',
                        help='MCP transport (default: streamable-http)')

    # Comment language (mirrors cli.py --lang; resolves via pydme.i18n.resolve_lang)
    parser.add_argument('--lang', choices=['zh_CN', 'en_US'], default=None,
                        help='action help language: zh_CN (default) or en_US; can also be '
                             'set via the DME_CLI_LANG environment variable')

    # Risk control (mirrors cli.py --accept-risk)
    parser.add_argument('--accept-risk', action='store_true',
                        help='accept risk by default: blacklisted high-risk operations are '
                             'allowed unless exec_action passes accept_risk=false')

    return parser


def parse_host_port(server: str) -> tuple:
    """Parse a host:port string, with IPv6 support ([::1]:8000)."""
    if server.startswith('['):  # IPv6
        host, _, port = server[1:].partition(']:')
    else:
        host, _, port = server.rpartition(':')
    if not host or not port.isdigit():
        raise ValueError(f'--mcp-server must be in host:port format, got: {server!r}')
    return host, int(port)


# ---------------------------------------------------------------------------
# Tool state and implementations (mirror cli.py behavior)
# ---------------------------------------------------------------------------

class _State:
    """Runtime state shared by the meta tools (client, blacklist, args, lang)."""

    def __init__(self, client, blacklist, args, lang):
        self.client = client
        self.blacklist = blacklist
        self.args = args
        self.lang = lang


def _risk_accepted(args) -> bool:
    """Whether risk has been accepted: ``--accept-risk`` or ``DME_ACCEPT_RISK=true/1/yes``
    (mirrors ``pydme.cli._accepts_risk``)."""
    return (getattr(args, 'accept_risk', False)
            or os.environ.get('DME_ACCEPT_RISK', '').lower() in ('true', '1', 'yes'))


def _resolve_action_key(state: _State, topic: str, action: str, subtopic: Optional[str]) -> str:
    """Resolve and validate (topic, subtopic, action) -> the full action key.

    - ``subtopic`` given: full key is ``<subtopic>_<action>``
    - ``subtopic`` omitted: only direct (two-level) actions are accepted
    """
    registry = load_actions_registry(state.lang)
    if topic not in registry:
        raise ValueError(f"topic '{topic}' not found")
    keys = registry[topic]
    if subtopic:
        key = f"{subtopic}_{action}"
        if key not in keys:
            raise ValueError(f"action '{topic} {subtopic} {action}' not found")
        return key
    entry = keys.get(action)
    if entry is None or entry['subtopic'] is not None:
        raise ValueError(f"action '{topic} {action}' not found (a subtopic may be required)")
    return action


def _list_topics(state: _State) -> List[Dict[str, Any]]:
    """All topics and subtopics with descriptions (same as `pydme --list-topics`)."""
    tree = load_actions(state.lang)['topics']
    result = []
    for topic in sorted(tree):
        data = tree[topic]
        result.append({
            'topic': topic,
            'description': data['description'],
            'subtopics': [
                {'name': st, 'description': sd['description']}
                for st, sd in sorted(data['subtopics'].items())
            ],
        })
    return result


def _list_actions(state: _State, topic: str, subtopic: Optional[str] = None) -> List[Dict[str, Any]]:
    """Actions of a topic or one of its subtopics (same as `pydme <topic> [<subtopic>] --help`).

    Without ``subtopic``: direct actions first, then every subtopic's actions.
    """
    tree = load_actions(state.lang)['topics']
    if topic not in tree:
        raise ValueError(f"topic '{topic}' not found")
    data = tree[topic]
    result = []
    if subtopic:
        sd = data['subtopics'].get(subtopic)
        if sd is None:
            raise ValueError(f"subtopic '{subtopic}' not found in topic '{topic}'")
        result.extend({'subtopic': subtopic, 'action': a['name'], 'description': a['description']}
                      for a in sd['actions'])
    else:
        result.extend({'subtopic': None, 'action': a['name'], 'description': a['description']}
                      for a in data['actions'])
        for st, sd in sorted(data['subtopics'].items()):
            result.extend({'subtopic': st, 'action': a['name'], 'description': a['description']}
                          for a in sd['actions'])
    return result


def _show_action_help(state: _State, topic: str, action: str,
                      subtopic: Optional[str] = None) -> Dict[str, Any]:
    """Help of one action: description, parameters (without ``client``) and output
    (same as `pydme <topic> [<subtopic>] <action> --help`)."""
    registry = load_actions_registry(state.lang)
    key = _resolve_action_key(state, topic, action, subtopic)
    doc = get_action_doc(topic, key, state.lang)
    if doc is None:  # mirror cli.py _doc_for: fall back to the function docstring
        func = registry[topic][key]['func']
        doc = inspect.getdoc(func) or ''
    # parse with the SDK's own docstring parser so the output matches
    # `pydme <topic> [<subtopic>] <action> --help` exactly
    parsed = DMECLI().parse_docstring(doc)
    params = {name: desc for name, desc in parsed['params'].items() if name != 'client'}
    return {
        'action': f"{topic} {subtopic} {action}" if subtopic else f"{topic} {action}",
        'description': parsed['description'],
        'params': params,
        'returns': parsed['returns'],
    }


def _exec_action(state: _State, topic: str, action: str, params: Optional[Dict[str, Any]] = None,
                 subtopic: Optional[str] = None, accept_risk: Optional[bool] = None) -> Any:
    """Execute one action with a parameter dict (same as
    `pydme <topic> [<subtopic>] <action> --param value ...`).

    Blacklisted high-risk operations are rejected unless ``accept_risk=true``
    (or the server was started with --accept-risk / DME_ACCEPT_RISK).
    """
    registry = load_actions_registry(state.lang)
    key = _resolve_action_key(state, topic, action, subtopic)
    entry = registry[topic][key]
    # risk check: the blacklist comes from pydme.cli.load_blacklist
    # (user ~/.config/pydme/blacklist.json takes precedence over the SDK default)
    if key in state.blacklist.get(topic, []):
        if accept_risk is None:
            accept_risk = _risk_accepted(state.args)
        if not accept_risk:
            return {
                'error': f'high-risk operation rejected: {topic} {key}; '
                         'set accept_risk=true to allow this operation',
                'code': 'RISK_BLOCKED',
            }
    func = entry['func']
    result = func(state.client, **(params or {}))
    return result if result is not None else {'result': 'ok'}


# ---------------------------------------------------------------------------
# Tool registration (4 meta tools)
# ---------------------------------------------------------------------------

def _register_tools(mcp: FastMCP, state: _State) -> None:
    """Register the 4 meta tools on the MCP server."""

    def list_topics() -> List[Dict[str, Any]]:
        return _list_topics(state)

    mcp.add_tool(
        list_topics,
        name='list_topics',
        description='List all topics and subtopics with descriptions '
                    '(equivalent to `pydme --list-topics`)',
        annotations=ToolAnnotations(topic='', subtopic='', action='list_topics'),
        structured_output=True,
    )

    def list_actions(
        topic: Annotated[str, Field(description='topic name, e.g. storage, san, nas')],
        subtopic: Annotated[Optional[str], Field(
            description='subtopic name, optional; if omitted, lists the direct actions '
                        'and the actions of every subtopic under the topic')] = None,
    ) -> List[Dict[str, Any]]:
        return _list_actions(state, topic, subtopic)

    mcp.add_tool(
        list_actions,
        name='list_actions',
        description='List the actions of a topic or one of its subtopics with descriptions '
                    '(equivalent to `pydme <topic> [<subtopic>] --help`)',
        annotations=ToolAnnotations(topic='', subtopic='', action='list_actions'),
        structured_output=True,
    )

    def show_action_help(
        topic: Annotated[str, Field(description='topic name, e.g. storage, san, nas')],
        action: Annotated[str, Field(description='action name, e.g. list, create, delete, show')],
        subtopic: Annotated[Optional[str], Field(
            description='subtopic name, optional; required for three-level actions')] = None,
    ) -> Dict[str, Any]:
        return _show_action_help(state, topic, action, subtopic)

    mcp.add_tool(
        show_action_help,
        name='show_action_help',
        description='Show the help of one action: description, parameter list and output '
                    '(equivalent to `pydme <topic> [<subtopic>] <action> --help`)',
        annotations=ToolAnnotations(topic='', subtopic='', action='show_action_help'),
        structured_output=True,
    )

    def exec_action(
        topic: Annotated[str, Field(description='topic name, e.g. storage, san, nas')],
        action: Annotated[str, Field(description='action name, e.g. list, create, delete, show')],
        params: Annotated[Optional[Dict[str, Any]], Field(
            description='action parameters as a JSON object, e.g. {"limit": 20}')] = None,
        subtopic: Annotated[Optional[str], Field(
            description='subtopic name, optional; required for three-level actions')] = None,
        accept_risk: Annotated[Optional[bool], Field(
            description='set to true to allow a blacklisted high-risk operation '
                        '(defaults to the server --accept-risk / DME_ACCEPT_RISK setting)')] = None,
    ) -> Any:
        return _exec_action(state, topic, action, params, subtopic, accept_risk)

    mcp.add_tool(
        exec_action,
        name='exec_action',
        description='Execute one action with the given parameters '
                    '(equivalent to `pydme <topic> [<subtopic>] <action> --param value ...`); '
                    'high-risk operations are blocked unless accept_risk=true',
        annotations=ToolAnnotations(topic='', subtopic='', action='exec_action'),
    )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def run_mcp_server(args, client):
    """Build and run the MCP server with the 4 meta tools."""
    blacklist = load_blacklist()  # pydme.cli.load_blacklist (mirrors the pydme CLI risk policy)
    # Action help language: --lang > DME_CLI_LANG > zh_CN (mirrors cli.py)
    lang = resolve_lang(args.lang)
    state = _State(client, blacklist, args, lang)

    if args.mcp_transport == 'stdio':
        mcp = FastMCP('dme')
        _register_tools(mcp, state)
        print('MCP Server (stdio) started, 4 meta tools registered '
              '(list_topics / list_actions / show_action_help / exec_action)', file=sys.stderr)
        mcp.run(transport='stdio')
        return

    host, port = parse_host_port(args.mcp_server)
    mcp = FastMCP('dme', streamable_http_path='/mcp/v1')
    _register_tools(mcp, state)
    print(f'MCP Server started: http://{host}:{port}/mcp/v1  '
          f'(meta tools: list_topics, list_actions, show_action_help, exec_action)',
          file=sys.stderr)
    uvicorn.run(mcp.streamable_http_app(), host=host, port=port, log_level='info')


def main():
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()

    # Enter MCP mode when --mcp-server is set or --mcp-transport stdio is explicit
    if not args.mcp_server and args.mcp_transport != 'stdio':
        parser.print_help()
        sys.exit(1)

    if args.mcp_transport == 'streamable-http':
        try:
            parse_host_port(args.mcp_server)
        except ValueError as e:
            parser.error(str(e))

    # Initialize DMEAPIClient (same logic as cli.py)
    endpoint = args.endpoint or os.environ.get('DME_API_ENDPOINT')
    username = args.user or os.environ.get('DME_API_USERNAME')
    password = args.password or os.environ.get('DME_API_PASSWORD')
    auth_token = args.token or os.environ.get('DME_API_AUTH_TOKEN')

    if not auth_token and not (endpoint and username and password):
        print('error: must provide endpoint, user and password arguments, or use --token with an auth token')
        print('can be set via --endpoint, --user, --password, --token or environment variables')
        sys.exit(1)

    client = DMEAPIClient(
        endpoint=endpoint,
        username=username,
        password=password,
        auth_token=auth_token,
        timeout=args.timeout,
        cache_token=args.cache_auth_token,
    )

    # Check whether the client already has a token; log in if not
    if not client.headers.get('X-Auth-Token'):
        print(f'connecting to DME: {endpoint}')
        client.login()

    run_mcp_server(args, client)


if __name__ == '__main__':
    main()
