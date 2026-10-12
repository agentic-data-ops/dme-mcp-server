"""Standalone docstring parser for action functions.

Rewritten from the ``parse_docstring`` rules in ``pydme/cli.py`` (without importing cli).
The parse result feeds ``show_action_help`` in server_v1.py:

- ``description``   <- function description before Args (clean text)
- ``params``        <- Args parameter descriptions (``client`` excluded by the caller)
- ``returns``       <- Returns block text
"""

from typing import Any, Dict, List

import re

# Markers that trigger a nested format block (fields inside the block are folded
# into the current parameter description). Supports both the Chinese (default
# branch) and English (main-en branch) docstrings of dme-python-sdk, including
# prefixed variants (e.g. 'FC_SAN parameter format:' / its Chinese equivalent,
# which still contain the base marker as a substring).
# NOTE: these literals must match the SDK docstrings verbatim - do not translate.
_BLOCK_MARKERS = (
    # Chinese docstrings (default branch)
    '参数格式如下：',
    '属性格式如下：',
    # English docstrings (main-en branch)
    'parameter format:',
    'attribute format:',
)


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
            if any(marker in stripped for marker in _BLOCK_MARKERS):
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
