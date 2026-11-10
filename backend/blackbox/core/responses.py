"""Concise, deterministic interpretations of measured evidence, never permissions."""
import re
from ..storage.files import clean_text


def grounded_timing(content, tools):
    """Do not publish model-invented observation timestamps or HTTP timings.

    Tool timestamps are supplied literally in UTC. HTTP request latency is not
    measured by web.inspect; total executor runtime must not be passed off as it.
    This validates those metadata fields, not arbitrary natural-language claims.
    """
    timestamps=[t['observed_at_utc'] for t in tools if t.get('observed_at_utc')]
    lines=[]
    for line in content.splitlines(keepends=True):
        normalized=line.replace('\u2011','-').replace('\u2013','-')
        if re.search(r'\b(timestamp|observation time)\b|\bobserved at\s*(?:[:|]|\d{4})',normalized,re.I):
            if not any(value in normalized for value in timestamps):
                continue
        if re.search(r'\b(elapsed(?:[_ -]\w+)?|latency|response[_ -]time)\b',normalized,re.I):
            continue
        lines.append(line)
    return ''.join(lines)


def cell(value):
    return clean_text(str(value)).replace('|', r'\|').replace('\n', ' ')


def evidence_summary(evidence, include_headers=False):
    rows=[]
    for item in evidence[:32]:
        if item.get('url') and item.get('status') is not None:
            rows.append((item['url'],f"HTTP {item['status']}"))
            headers=item.get('headers',{})
            if include_headers:
                if isinstance(headers,str):
                    headers=dict(line.split(':',1) for line in headers.splitlines() if ':' in line)
                if isinstance(headers,dict):
                    for name,value in headers.items():
                        if name.strip().lower() in {'server','content-type','location','strict-transport-security','content-security-policy','x-frame-options','x-content-type-options','cache-control','content-length'}:
                            rows.append((name.strip(),str(value).strip()))
        elif item.get('kind')=='host':
            rows.append((item.get('target','Host'),item.get('state') or 'Observed host label'))
        elif item.get('kind')=='port':
            rows.append((f"{item.get('target','')}:{item.get('port','')}/{item.get('protocol','')}",
                         f"{item.get('state','unknown')} {item.get('service','')}".strip()))
        elif item.get('findings') is not None:
            label=f"{item.get('tool', 'Imported output')} — {item.get('freshness','unverified')} / {item.get('status',item.get('source','import'))}"
            rows.append((item.get('evidence_id') or item.get('id','Stored record'),label))
            for finding in item['findings'][:8]:
                if finding.get('url') or finding.get('port'):
                    rows.append((finding.get('url') or f"{finding.get('target','')}:{finding['port']}",
                                 f"HTTP {finding['status']}" if finding.get('status') else finding.get('state','Unverified candidate')))
        elif item.get('path'):
            rows.append((item['path'],'Observed file/path'))
        elif item.get('tls_output') or item.get('technologies_output'):
            rows.append((item.get('url','Inspection'),(item.get('tls_output') or item.get('technologies_output'))[:1200]))
    if not rows:
        return 'No structured findings were returned; raw output is retained in Live Activity.'
    return '| Observation | Measured result |\n| --- | --- |\n'+'\n'.join(f'| {cell(a)} | {cell(b)} |' for a,b in rows[:40])


def milestone(name,result):
    if not result.ok:
        return result.summary
    if name=='net.scan_ports':
        opened=[f"{f['port']}/{f.get('protocol','tcp')} ({f.get('service') or 'unknown service'})"
                for f in result.evidence if f.get('kind')=='port' and f.get('state')=='open']
        return 'Observed open ports: '+', '.join(opened[:8]) if opened else 'No open ports were reported in the selected scan output.'
    return result.summary


def host_summary(observation):
    facts=observation['facts']
    distribution=facts.get('distribution') or {}
    cpu=facts.get('cpu') or {}
    memory=facts.get('memory') or {}
    gpus=facts.get('gpus')
    rows=[('Operating system',distribution.get('PRETTY_NAME') or facts.get('os')),
          ('Kernel',facts.get('kernel')),('Architecture',facts.get('architecture')),
          ('CPU',cpu.get('model')),('Logical CPUs',cpu.get('logical_cpus')),
          ('Total memory (bytes)',memory.get('total_bytes')),
          ('GPU',', '.join(gpu.get('name','Unknown') for gpu in gpus) if gpus else None)]
    text='| Measured field | Observation |\n| --- | --- |\n'
    text+='\n'.join(f'| {cell(label)} | {cell(value) if value is not None else "Not measured"} |' for label,value in rows)
    return text+f"\n\nEvidence: `{observation['tool_id']}`; observed at {observation['timestamp']}."
