"""Shared private evidence handling and independently checked agent contracts."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

SENSITIVE = re.compile(r'(?i)^(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|authorization|cookie|set-cookie|token|bearer|pairing|images)$')


def sanitize(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if SENSITIVE.fullmatch(str(k)) else sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(('{','[')):
        try:
            nested = json.loads(value)
            if isinstance(nested, (dict,list)):
                return json.dumps(sanitize(nested))
        except ValueError:
            pass
    value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)', '[REDACTED PRIVATE KEY]', value, flags=re.S)
    value = re.sub(r'(?i)(Bearer\s+)[\w.~+/=-]+', r'\1[REDACTED]', value)
    value = re.sub(r'(?i)(["\']?(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|authorization|token|bearer|pairing)["\']?\s*[:=]\s*)(?:["\'][^"\'\n]*["\']|[^\s,;}]+)', r'\1[REDACTED]', value)
    value = re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', value)
    value = re.sub(r'([?#](?:pair|token|key)=)[^\s"\']+', r'\1[REDACTED]', value, flags=re.I)
    # Never enumerate a home directory. Strip incidental local path references too.
    value = value.replace(str(Path.home()), '<HOME>')
    for key, secret in os.environ.items():
        if re.search(r'(?i)(?:token|secret|password|api_key)$', key) and len(secret) >= 8:
            value = value.replace(secret, '[REDACTED]')
    return value


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(sanitize(value), indent=2, default=str) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def agent_findings(events, tools, *, required_tools=(), expected_text=None, final_text='', terminal=None):
    """Explicit fixture expectations, never another model, decide whether a run passed."""
    findings = []
    successful = {t['tool_name'] for t in tools if t['status'] == 'success'}
    for name in required_tools:
        if name not in successful:
            findings.append({'category': 'missing_tool_call', 'detail': f'No successful {name} executor record'})
    if expected_text and expected_text not in final_text:
        findings.append({'category': 'fabricated_tool_output', 'detail': 'Answer does not contain the independently planted observation'})
    if terminal == 'completed' and any(e['type'] == 'run_failed' for e in events):
        findings.append({'category': 'premature_completion', 'detail': 'Completed status contradicts failure journal'})
    finished = {e.get('tool_id'): e for e in events if e['type'] == 'tool_finished'}
    for tool in tools:
        event = finished.get(tool['id'])
        if not event or event.get('status') != tool['status']:
            findings.append({'category': 'fabricated_tool_output', 'detail': 'Tool journal and executor ledger disagree'})
    if terminal == 'cancelled' and not any(e['type'] == 'run_cancelled' for e in events):
        findings.append({'category': 'broken_cancellation', 'detail': 'Cancelled state lacks cancellation evidence'})
    return findings
