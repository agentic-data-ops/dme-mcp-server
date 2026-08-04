"""Standalone docstring parser for action functions.

Rewritten from the ``parse_docstring`` rules in ``pydme/cli.py`` (without importing cli),
and converts the parse result into the three elements of an MCP tool:

- ``description``   <- function description before Args (clean text)
- ``inputSchema``   <- Args parameter descriptions (injected by server_v1.py as ``Annotated[type, Field(description=...)]``)
- ``outputSchema``  <- heuristic parse of Returns top-level fields -> dynamic pydantic model
  (``structured_output``); ``Map<K, V>`` fields become open objects, and a bare-{ block
  following a field (e.g. ``data: ... (Map<object, SimpleIndicator>),`` + indented
  ``{ ... }``) nests its fields under that field (map value structure)
"""

from typing import Any, Dict, List, Optional, Tuple

import re

from pydantic import BaseModel, ConfigDict, Field, create_model

# docstring return type hints -> JSON Schema types
_TYPE_MAP = {
    'int': 'integer',
    'int32': 'integer',
    'int64': 'integer',
    'integer': 'integer',
    'string': 'string',
    'char': 'string',
    'varchar': 'string',
    'str': 'string',
    'boolean': 'boolean',
    'bool': 'boolean',
    'double': 'number',
    'float': 'number',
    'number': 'number',
}

# JSON Schema types -> pydantic field types
_SCHEMA_TO_PY = {
    'integer': int,
    'number': float,
    'boolean': bool,
    'array': list,
    'object': dict,
}

# Markers that trigger a nested format block (fields inside the block are not
# top-level and are skipped during field extraction). Supports both the Chinese
# (default branch) and English (main-en branch) docstrings of dme-python-sdk,
# including prefixed variants (e.g. 'FC_SAN parameter format:' / its Chinese
# equivalent, which still contain the base marker as a substring).
# NOTE: these literals must match the SDK docstrings verbatim - do not translate.
_BLOCK_MARKERS = (
    # Chinese docstrings (default branch)
    '参数格式如下：',
    '属性格式如下：',
    # English docstrings (main-en branch)
    'parameter format:',
    'attribute format:',
)

# Returns top-level field line: name: description (type) with anything trailing;
# the description group stops greedily at '(' or '{'. The character class [,，。.]
# is functional - it tolerates trailing commas/periods (Chinese full-width or
# English half-width) in both SDK docstring variants.
_FIELD_RE = re.compile(r'^\s*(\w+)\s*:\s*([^({]*)(?:\(([^)]*)\))?\s*[,，。.]?.*$')


def parse_docstring(doc: str) -> Dict[str, Any]:
    """Parse a function docstring.

    Args:
        doc: the function docstring

    Returns:
        ``{'description': str, 'params': {name: desc}, 'returns': str}``
    """
    result = {'description': '', 'params': {}, 'returns': ''}
    if not doc:
        return result

    lines = doc.strip().split('\n')

    # 1) description: everything before Args/Returns, minus base indent (relative indent kept)
    desc_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(('Args:', 'Returns:')):
            break
        if stripped:
            desc_lines.append(line)
    if desc_lines:
        base = len(desc_lines[0]) - len(desc_lines[0].lstrip())
        formatted = []
        for line in desc_lines:
            indent = len(line) - len(line.lstrip())
            formatted.append(' ' * max(0, indent - base) + line.strip())
        result['description'] = '\n'.join(formatted)

    # 2) params: 'name: desc' lines inside the Args section; format blocks
    #    ({ } depth) are folded into the current parameter description
    in_args = False
    current = None
    param_lines: Dict[str, List[str]] = {}
    depth = 0
    for line in lines:
        stripped = line.strip()
        if not in_args:
            if stripped.startswith('Args:'):
                in_args = True
            continue
        if stripped.startswith(('Returns:', 'Raises:', 'Note:', 'Example:')):
            break

        if depth > 0:
            depth += stripped.count('{') - stripped.count('}')
            if depth < 0:
                depth = 0
            if current is not None:
                param_lines[current].append(stripped)
            continue

        m = re.match(r'^(\w+)\s*:\s*(.+)$', stripped)
        if m:
            current = m.group(1)
            param_lines[current] = [m.group(2)]
            # Functional literals: match the SDK docstring block markers (zh/en)
            if any(m in stripped for m in _BLOCK_MARKERS):
                depth = stripped.count('{') - stripped.count('}')
                if depth < 0:
                    depth = 0
        elif current and stripped:
            param_lines[current].append(stripped)

    result['params'] = {k: '\n'.join(v) for k, v in param_lines.items()}

    # 3) returns: from 'Returns:' up to Raises/Note/Example
    in_returns = False
    returns = ''
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('Returns:'):
            in_returns = True
            rest = stripped[len('Returns:'):].strip()
            returns = rest if rest else ''
            continue
        if in_returns:
            if stripped.startswith(('Raises:', 'Note:', 'Example:')):
                break
            if stripped:
                returns = f'{returns}\n{stripped}' if returns else stripped
    result['returns'] = returns

    return result


def returns_is_array(returns_text: str) -> bool:
    """Whether the Returns block is an array literal (first non-empty line starts with '[').

    Matches SDK array-style Returns like ``[{...}, ...]`` (e.g. ``task_show``); object-style
    Returns like ``{...}`` (e.g. ``task_list``) return False.
    """
    for raw in returns_text.split('\n'):
        stripped = raw.strip()
        if not stripped:
            continue
        return stripped.startswith('[')
    return False


def parse_returns_fields(returns_text: str) -> List[Tuple[str, str, str, List[Any]]]:
    """Heuristically parse the Returns fields -> ``[(name, type_hint, desc, children)]``.

    Top-level determination: the markers in ``_BLOCK_MARKERS`` (the Chinese and
    English format-block markers used by the SDK docstrings) start a nested block
    ({ } depth); lines inside such blocks are not extracted. Lines at depth 0 are
    parsed as ``name: description (type)``; a missing type yields ''.

    A bare ``{`` block that immediately follows a field line (e.g. ``data: ... (Map<...>),``
    plus an indented ``{ ... }`` in the SDK docstrings) is collected recursively into that
    field's ``children`` instead of leaking its fields to top level. The same applies to a
    marker block on a ``Map``-typed field (e.g. ``data: ... (Map<object, SimpleIndicator>)。属性格式如下：{ ... }``):
    the block describes the map VALUE structure and is collected as children rather than
    skipped. Marker blocks on non-Map fields (e.g. ``List<...>`` element layouts) are still
    skipped. The marker literals must match the SDK docstrings verbatim - do not translate.
    """
    fields: List[Any] = []
    if not returns_text:
        return fields

    def freeze(entries: List[Any]) -> List[Tuple[str, str, str, List[Any]]]:
        return [(n, t, d, freeze(c)) for n, t, d, c in entries]

    # stack of parent field lists for bare-{ blocks; ``current`` is the list being filled
    stack: List[List[Any]] = []
    current = fields
    pending: Any = None  # the field entry most recently parsed, waiting for a bare '{'
    skip_depth = 0  # { } depth of marker blocks whose lines are skipped

    for raw in returns_text.split('\n'):
        stripped = raw.strip()
        if not stripped:
            continue

        is_block_marker = any(m in stripped for m in _BLOCK_MARKERS)

        if skip_depth > 0:
            # inside a marker block: skip everything (including nested marker lines,
            # which would otherwise leak their fields to the current level) and just
            # track the { } depth until the block closes.
            skip_depth += stripped.count('{') - stripped.count('}')
            if skip_depth < 0:
                skip_depth = 0
            continue

        if stripped.startswith('}') and stack:
            current = stack.pop()
            pending = None
            continue

        # a bare '{' right after a parsed field opens that field's children block
        if stripped.startswith('{') and pending is not None:
            stack.append(current)
            current = pending[3]
            pending = None
            continue

        m = _FIELD_RE.match(stripped)
        if m:
            name = m.group(1)
            # strip trailing period/comma (Chinese full-width or English half-width)
            desc = (m.group(2) or '').strip().rstrip('。.,，')
            type_hint = (m.group(3) or '').strip()
            entry: List[Any] = [name, type_hint, desc, []]
            current.append(entry)
            pending = entry
        else:
            pending = None

        if is_block_marker:
            # Map-typed fields: the marker block describes the map VALUE structure
            # (e.g. ``data: ... (Map<object, SimpleIndicator>)。属性格式如下：{ ... }``),
            # so collect its fields as children (like a bare-{ block) instead of skipping.
            if pending is not None and pending[1].strip().lower().startswith('map<'):
                stack.append(current)
                current = pending[3]
                pending = None
            else:
                skip_depth += stripped.count('{') - stripped.count('}')
                if skip_depth < 0:
                    skip_depth = 0

    return freeze(fields)


def _schema_type(type_hint: str) -> Dict[str, Any]:
    """Map a type hint to a JSON Schema type fragment.

    Generic prefixes (``List<...>`` / ``Map<...>``, case-insensitive) are checked
    against the full hint BEFORE truncating at the first comma, because their
    type arguments may themselves contain commas (e.g. ``Map<object, SimpleIndicator>``).
    ``Map<K, V>`` maps to ``type: object`` (``additionalProperties`` carries the
    value type when it is a recognizable scalar; otherwise the map is left open).
    Non-generic hints are truncated at the first comma so trailing annotations like
    ``'integer, UTC milliseconds'`` or ``'string, nullable'`` map to their base
    type instead of falling through to an untyped ``str`` field.
    """
    hint = type_hint.strip()
    if not hint:
        return {}
    lower = hint.lower()
    if lower.startswith('list<'):
        item_hint = hint[5:-1].strip()
        item_type = _TYPE_MAP.get(item_hint.lower())
        return {'type': 'array', 'items': {'type': item_type} if item_type else {}}
    if lower.startswith('map<'):
        inner = hint[4:-1].strip()
        parts = [p.strip() for p in inner.split(',', 1)]
        value_hint = parts[1] if len(parts) > 1 else parts[0]
        value_type = _TYPE_MAP.get(value_hint.lower())
        return {
            'type': 'object',
            'additionalProperties': {'type': value_type} if value_type else {},
        }
    base = hint.split(',')[0].strip()
    t = _TYPE_MAP.get(base.lower())
    return {'type': t} if t else {}


def build_output_model(fields: List[Tuple[str, str, str, List[Any]]], is_array: bool = False) -> Any:
    """Build a lenient pydantic model from the Returns top-level fields (for outputSchema).

    - All fields are Optional with default None (tolerates missing fields in real returns)
    - ``extra='allow'`` (tolerates extra fields in real returns)
    - Returns None when ``fields`` is empty (caller falls back to unstructured output)
    - Returns ``List[element_model]`` when ``is_array=True`` (array-style Returns like
      ``[{...}, ...]``). MCP V1 requires ``CallToolResult.structuredContent`` to be a JSON
      object, so FastMCP wraps array-typed returns into ``{'result': [...]}`` and reports
      an object outputSchema whose ``properties.result`` carries the array schema
      (top-level ``type: array`` is not protocol-compliant); otherwise returns the plain
      ``element_model`` (object)
    """
    if not fields:
        return None

    element_model = _build_element_model('ActionOutput', fields)
    if is_array:
        # FastMCP wraps List[model] returns into {'result': [...]} (MCP V1 requires
        # structuredContent to be a JSON object, so a top-level array is not allowed).
        return List[element_model]
    return element_model


def _build_element_model(model_name: str, fields: List[Tuple[str, str, str, List[Any]]]) -> Any:
    """Recursively build a pydantic model for one level of Returns fields.

    A ``Map``-typed field with parsed ``children`` (the bare-{ block in the docstring,
    e.g. ``Map<object, SimpleIndicator>``) becomes ``Dict[str, <children model>]`` so the
    outputSchema carries the map value structure via ``additionalProperties``.
    """
    model_fields: Dict[str, Any] = {}
    for name, type_hint, desc, children in fields:
        schema_t = _schema_type(type_hint)
        if schema_t.get('type') == 'object' and children:
            child_model = _build_element_model(f'{name}Item', children)
            model_fields[name] = (
                Optional[Dict[str, child_model]],
                Field(None, description=desc or type_hint or name),
            )
            continue
        py_type = _SCHEMA_TO_PY.get(schema_t.get('type'), str)
        if schema_t.get('type') == 'array':
            py_type = list
        model_fields[name] = (Optional[py_type], Field(None, description=desc or type_hint or name))

    return create_model(model_name, __config__=ConfigDict(extra='allow'), **model_fields)
