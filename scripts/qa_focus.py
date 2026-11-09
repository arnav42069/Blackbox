"""Deterministic repair targets and bounded context; never ask a model to grade tests."""
from __future__ import annotations

import ast
from pathlib import Path
import re

from scripts.qa_support import sanitize


REPAIR_SCHEMA = {'type': 'object', 'properties': {'edits': {'type': 'array', 'items': {
    'type': 'object', 'properties': {key: {'type': 'string'} for key in ('path', 'old', 'new')},
    'required': ['path', 'old', 'new'], 'additionalProperties': False}}},
    'required': ['edits'], 'additionalProperties': False}


def python_dependencies(workspace, name, nodeid=None):
    """Local imports only, including from-imported modules; no code execution."""
    path = workspace / name
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError, UnicodeError):
        return set()
    selected = tree
    for part in (nodeid or '').split('::')[1:]:
        identifier = part.split('[', 1)[0]
        match = next((child for child in getattr(selected, 'body', [])
                      if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                      and child.name == identifier), None)
        if match is None:
            selected = tree
            break
        selected = match
    used = {node.id for node in ast.walk(selected) if isinstance(node, ast.Name)}
    found = set()
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names if (alias.asname or alias.name.split('.')[0]) in used]
        elif isinstance(node, ast.ImportFrom):
            aliases = [alias for alias in node.names if alias.name == '*' or (alias.asname or alias.name) in used]
            if not aliases:
                continue
            prefix = node.module or ''
            if node.level:
                package = Path(name).parent.parts
                prefix = '.'.join((*package[:len(package) - node.level + 1], prefix)).strip('.')
            modules = [prefix, *(prefix + '.' + alias.name for alias in aliases)]
        for module in modules:
            relative = module.replace('.', '/')
            for base in ('', 'backend/'):
                for suffix in ('.py', '/__init__.py'):
                    candidate = base + relative + suffix
                    if (workspace / candidate).is_file():
                        found.add(candidate)
    return found


def failure_groups(results):
    groups = {}
    for result in results:
        if result['status'] != 'failed':
            continue
        nodeid = result['id']
        if nodeid.startswith('tests/') and '.py::' in nodeid:
            kind, target = 'pytest', nodeid.split('::')[0]
        elif nodeid.startswith('e2e:') and result.get('file') and result.get('line'):
            kind, target = 'browser', result['file']
        elif nodeid in {'typecheck', 'build'}:
            kind, target = 'frontend', 'frontend-checks'
        else:
            kind, target = 'intervention', nodeid
        group = groups.setdefault((kind, target), {'id': kind + ':' + target, 'kind': kind,
                                                  'target': target, 'failures': [], 'attempts': 0})
        group['failures'].append(result)
    return list(groups.values())


def relevant_sources(workspace, failures):
    """Hints, not an edit allowlist. Tracebacks/diagnostics precede direct imports."""
    found = set()
    for result in failures:
        text = str(result.get('detail', ''))
        found.update(re.findall(r'((?:backend/blackbox|frontend/src|scripts)/[\w/.-]+\.(?:py|tsx?|css))', text))
        # TypeScript diagnostics are relative to frontend/.
        found.update('frontend/' + name for name in re.findall(r'\b(src/[\w/.-]+\.(?:tsx?|css))', text))
        if result['id'].startswith('tests/'):
            found.update(python_dependencies(workspace, result['id'].split('::')[0], result['id']))
        elif result['id'].startswith('e2e:'):
            found.update(('frontend/src/app.tsx', 'frontend/src/styles.css'))
    return sorted(name for name in found if '..' not in Path(name).parts and (workspace / name).is_file())[:12]


def concise_failures(failures, limit=12000):
    """Keep exact IDs separately; bound diagnostics even for huge parametrized groups."""
    report = []
    remaining = limit
    for result in failures:
        if remaining <= 0:
            break
        detail = str(sanitize(result.get('detail', '')))[-min(1800, remaining):]
        report.append({'id': result['id'], 'category': result['category'], 'detail': detail})
        remaining -= len(detail) + len(result['id']) + 100
    return report


def failure_excerpts(workspace, failures, sources, limit=6000):
    """Preload small exact oracles/source files to avoid separate worker read turns."""
    excerpts = {}
    remaining = limit
    for failure in failures:
        nodeid = failure['id']
        if not nodeid.startswith('tests/') or '.py::' not in nodeid:
            continue
        name, *parts = nodeid.split('::')
        try:
            source = (workspace / name).read_text()
            selected = ast.parse(source)
            for part in parts:
                selected = next(child for child in selected.body
                                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                                and child.name == part.split('[', 1)[0])
            excerpt = ast.get_source_segment(source, selected)
        except (OSError, SyntaxError, StopIteration, AttributeError, UnicodeError):
            continue
        if excerpt and len(excerpt) <= min(2000, remaining) and nodeid not in excerpts:
            excerpts[nodeid] = excerpt
            remaining -= len(excerpt)
    for name in sources:
        try:
            source = (workspace / name).read_text()
        except (OSError, UnicodeError):
            continue
        if len(source) <= min(2400, remaining):
            excerpts[name] = source
            remaining -= len(source)
    return sanitize(excerpts)


def token_usage(events):
    """Cache hits are a subset of input tokens, never added a second time."""
    usage = [event['usage'] for event in events if event.get('type') == 'turn.completed'
             and isinstance(event.get('usage'), dict)]
    if not usage or any(not all(isinstance(item.get(key), int) and item[key] >= 0
                               for key in ('input_tokens', 'output_tokens')) for item in usage):
        return None
    totals = {key: sum(item.get(key, 0) for item in usage)
              for key in ('input_tokens', 'cached_input_tokens', 'output_tokens')}
    totals['total_tokens'] = totals['input_tokens'] + totals['output_tokens']
    return totals
