#!/usr/bin/env python
"""DME MCP Server (MCP V1).

Dynamically exposes the action functions of the 16 action modules from ``dme-python-sdk`` as MCP tools:

- Endpoint: a single root MCP Server at ``<endpoint>/mcp/v1`` exposing every module's action tools
  (no per-module mounts)
- Tool naming: dot-separated ``<topic>.<action_key>`` (SEP-986 allows ``.``)
- Annotation: every tool carries ``annotations.topic/subtopic/action`` (extension fields; no ``_meta``)
- Docstring parsing: standalone parser for action function docstrings ->
  ``description`` / ``inputSchema`` (parameter descriptions) / ``outputSchema`` (Returns fields)
- Blacklist: mirrors ``pydme/cli.py``; blacklisted high-risk operations are rejected by default
  (``--accept-risk`` / ``DME_ACCEPT_RISK`` to allow)
"""

import argparse
import importlib
import inspect
import os
import pkgutil
import sys
from typing import Annotated, Any, Dict, List, Optional, get_origin

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from pydme.client import DMEAPIClient

from dme_mcp_server.blacklist import accept_risk_enabled, is_risky, load_blacklist
from dme_mcp_server.docstring_parser import (
    build_output_model,
    parse_docstring,
    parse_returns_fields,
)


# ---------------------------------------------------------------------------
# Argument parsing (DME connection params mirror pydme/cli.py)
# ---------------------------------------------------------------------------

def create_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        prog='dme-mcp-server',
        description='DME MCP Server - exposes dme-python-sdk actions as MCP V1 tools',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''\
Examples:
  dme-mcp-server --mcp-server 0.0.0.0:8000 \\
      --endpoint https://192.168.1.100:26335 --user admin --password xxx
  # Environment variables: DME_MCP_SERVER / DME_API_ENDPOINT / DME_API_USERNAME / DME_API_PASSWORD
  # Endpoint: http://<host>:<port>/mcp/v1''',
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

    # Risk control (mirrors cli.py --accept-risk)
    parser.add_argument('--accept-risk', action='store_true',
                        help='accept risk by default: blacklisted high-risk tools default '
                             'accept_risk=true (clients may still override it per call)')

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
# Action module discovery and action collection (mirror cli.py's get_available_topics / get_topic_actions)
# ---------------------------------------------------------------------------

def discover_topics() -> List[str]:
    """Scan the pydme.actions package directory and return all topic module names."""
    import pydme.actions as actions_pkg
    path = os.path.dirname(actions_pkg.__file__)
    return sorted(m for _, m, _ in pkgutil.iter_modules([path]) if not m.startswith('_'))


def get_topic_actions(topic: str) -> Dict[str, Dict]:
    """Collect all actions of a topic module -> {action_key: {func, subtopic, description}}.

    Handles subtopic declarations in ACTIONS (``module`` field expansion, mirroring cli.py).
    """
    try:
        module = importlib.import_module(f'pydme.actions.{topic}')
    except ImportError:
        return {}

    actions = {}
    for action_key, action_data in getattr(module, 'ACTIONS', {}).items():
        func = action_data.get('func')
        if func is None and 'module' in action_data:
            # Subtopic declaration: expand actions from the submodule
            subtopic = action_data.get('subtopic')
            try:
                sub_module = importlib.import_module(action_data['module'])
                for sub_key, sub_data in getattr(sub_module, 'ACTIONS', {}).items():
                    if sub_data.get('subtopic') in (subtopic, action_key) and sub_data.get('func'):
                        actions[sub_key] = {
                            'func': sub_data['func'],
                            'subtopic': subtopic,
                            'description': sub_data.get('description', ''),
                        }
            except ImportError:
                pass
            continue
        if callable(func):
            actions[action_key] = {
                'func': func,
                'subtopic': action_data.get('subtopic'),
                'description': action_data.get('description', ''),
            }
    return actions


def strip_subtopic_prefix(action_key: str, subtopic: Optional[str]) -> str:
    """Strip the subtopic prefix from an action_key (space / underscore separated, mirroring cli.py)."""
    if subtopic:
        for prefix in (f'{subtopic} ', f'{subtopic}_'):
            if action_key.startswith(prefix):
                return action_key[len(prefix):]
    return action_key


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def _is_json_schema_type(ann: Any) -> bool:
    """Whether an annotation can safely produce a JSON Schema.

    Some SDK modules (fcswitch / storage) define ``def list(...)`` that shadows builtins.list,
    binding ``x: list`` annotations to the function object - function annotations make pydantic
    generate a Callable schema and recurse into its signature (including client: DMEAPIClient),
    which fails registration. Such annotations fall back to ``Any``.
    """
    if ann is Any or ann is inspect.Parameter.empty:
        return True
    if get_origin(ann) is not None:  # generics: List/Dict/Optional/Union/Annotated, etc.
        return True
    if isinstance(ann, type):
        return issubclass(ann, (str, int, float, bool, list, dict, tuple, bytes, set, type(None)))
    return False


def make_wrapper(func, client, topic, action_key, parsed, output_model, risky, args):
    """Build the wrapper function for an MCP tool (decorator over the SDK action).

    - Drops the ``client`` parameter and injects the instantiated DMEAPIClient via closure
    - Parameter descriptions come from docstring parsing (``Annotated[type, Field(description=...)]``)
    - Blacklist guard: risky tools get an extra optional ``accept_risk`` parameter whose
      default comes from the server CLI (``--accept-risk`` / ``DME_ACCEPT_RISK``). When a
      blacklisted tool is called with ``accept_risk=false`` (or unset with a false default),
      the call is rejected with a structured error telling the caller to set
      ``accept_risk=true`` explicitly.
    - Structured output: returns a dynamic model (extra='allow' tolerates real return differences)
    """
    sig = inspect.signature(func)
    new_params = []
    for name, p in sig.parameters.items():
        if name == 'client':
            continue
        ann = p.annotation if p.annotation is not inspect.Parameter.empty else Any
        if not _is_json_schema_type(ann):
            ann = Any
        pdesc = parsed.get('params', {}).get(name, '')
        if pdesc:
            ann = Annotated[ann, Field(description=pdesc)]
        new_params.append(p.replace(annotation=ann))

    # Blacklist decorator: inject an optional accept_risk parameter on risky tools only.
    default_accept = accept_risk_enabled(args) if risky else None
    if risky:
        new_params.append(inspect.Parameter(
            'accept_risk',
            inspect.Parameter.KEYWORD_ONLY,
            default=default_accept,
            annotation=Annotated[bool, Field(
                description='explicitly set to true to allow this high-risk operation '
                            f'(defaults to {"true" if default_accept else "false"} from the '
                            'server --accept-risk / DME_ACCEPT_RISK setting)',
            )],
        ))

    def wrapper(**kwargs):
        if risky:
            accept = kwargs.pop('accept_risk', default_accept)
            if not accept:
                return {
                    'error': f'high-risk operation rejected: {topic} {action_key}; '
                             'set accept_risk=true to allow this operation',
                    'code': 'RISK_BLOCKED',
                }
        result = func(client, **kwargs)
        if output_model is not None and isinstance(result, dict):
            return output_model(**result)
        return result

    wrapper.__signature__ = sig.replace(
        parameters=new_params,
        return_annotation=output_model if output_model is not None else Any,
    )
    wrapper.__annotations__ = {p.name: p.annotation for p in new_params}
    if output_model is not None:
        wrapper.__annotations__['return'] = output_model
    return wrapper


def register_action(mcp, topic, action_key, info, client, blacklist, args):
    """Register a single action as an MCP tool."""
    func = info['func']
    subtopic = info.get('subtopic')
    action = strip_subtopic_prefix(action_key, subtopic)
    parsed = parse_docstring(inspect.getdoc(func) or '')
    output_model = build_output_model(parse_returns_fields(parsed['returns']))
    risky = is_risky(blacklist, topic, action_key)

    wrapper = make_wrapper(func, client, topic, action_key, parsed, output_model, risky, args)
    tool_name = f'{topic}.{action_key}'

    mcp.add_tool(
        wrapper,
        name=tool_name,
        description=parsed['description'] or info.get('description', ''),
        annotations=ToolAnnotations(
            topic=topic,
            subtopic=subtopic or '',
            action=action,
        ),
        structured_output=output_model is not None,
    )


def register_topic(mcp, topic, client, blacklist, args):
    """Register all actions of a topic module (a single failure does not take down the server)."""
    failed = 0
    for action_key, info in get_topic_actions(topic).items():
        try:
            register_action(mcp, topic, action_key, info, client, blacklist, args)
        except Exception as e:
            failed += 1
            print(f'warning: failed to register {topic}.{action_key}: {type(e).__name__}: {e}',
                  file=sys.stderr)
    if failed:
        print(f'[{topic}] {failed} action(s) failed to register (skipped)', file=sys.stderr)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def run_mcp_server(args, client):
    """Build and run the MCP server."""
    blacklist = load_blacklist()
    topics = discover_topics()

    if args.mcp_transport == 'stdio':
        # stdio: single server, register everything (tool names carry the topic prefix, so they are unique)
        mcp = FastMCP('dme')
        for topic in topics:
            register_topic(mcp, topic, client, blacklist, args)
        print(f'MCP Server (stdio) started, registered {len(topics)} topic(s)', file=sys.stderr)
        mcp.run(transport='stdio')
        return

    # streamable-http: a single root MCP Server at /mcp/v1 exposing every module's
    # action tools (tool names carry the <topic>.<action_key> prefix, so they are unique).
    host, port = parse_host_port(args.mcp_server)
    mcp = FastMCP('dme', streamable_http_path='/mcp/v1')
    for topic in topics:
        register_topic(mcp, topic, client, blacklist, args)
    print(f'MCP Server started: http://{host}:{port}/mcp/v1  (topics: {", ".join(topics)})',
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
