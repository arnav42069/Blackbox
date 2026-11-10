import pytest

from blackbox.core.planner import classify, explicit_target
from blackbox.core.scans import parse_visible, parse_nmap, parse_ffuf, normalized_args


@pytest.mark.parametrize('text,intent', [
    ('Discover open ports on 127.0.0.1','ports'),
    ('Identify HTTP endpoints','http_discovery'),
    ('Fuzz the web server','http_discovery'),
    ('Inspect HTTP headers','headers'),
    ('Identify web technologies','technologies'),
    ('Test SQL injection in my authorized application','sql_injection'),
    ('Inspect TLS configuration','tls'),
    ('Analyze existing scan results','analyze'),
    ('Read an uploaded screenshot containing an Nmap scan','image'),
    ('Analyze existing ffuf JSON results','analyze'),
    ('Read screenshot containing ffuf output','image'),
    ('Read screenshot and fuzz the identified HTTP endpoint','http_discovery'),
    ('127.0.0.1','general'),
    ('Execute commands on my authorized remote host','remote_access'),
])
def test_intent(text, intent):
    assert classify(text) == intent


def test_visible_nmap_does_not_invent_http_or_missing_host():
    findings = parse_visible('Nmap scan report for 127.0.0.1\n8080/tcp open http\n22/tcp filtered ssh\n9000/tcp open unknown\nError: connection refused\nHTTP/1.1 403 Forbidden')
    assert [f.url for f in findings if f.url] == ['http://127.0.0.1:8080/']
    assert next(f for f in findings if f.port==22).state == 'filtered'
    assert next(f for f in findings if f.kind=='http_status').status == 403
    assert next(f for f in findings if f.kind=='error').quote == 'Error: connection refused'
    assert not parse_visible('8080/tcp open http')
    assert not any(f.url for f in parse_visible('Nmap scan report for 127.0.0.1\n8080/tcp open unknown'))


def test_parsers():
    xml = '<nmaprun><host><address addr="127.0.0.1"/><ports><port portid="8443" protocol="tcp"><state state="open"/><service name="http" tunnel="ssl"/></port></ports></host><runstats><finished exit="success"/></runstats></nmaprun>'
    findings, complete = parse_nmap(xml)
    assert complete and findings[1].url == 'https://127.0.0.1:8443/'
    assert not parse_nmap(xml.replace('<runstats><finished exit="success"/></runstats>', ''))[1]
    with pytest.raises(ValueError):
        parse_nmap('<!ENTITY evil "value"><nmaprun/>')
    with pytest.raises(ValueError):
        parse_nmap('<nmaprun>')
    for output in ['{"results":[{"url":"http://127.0.0.1:8080/admin","status":403}]}', '{"url":"http://127.0.0.1:8080/admin","status":403}\n']:
        result = parse_ffuf(output)
        assert len(result)==1 and result[0].status==403 and result[0].port==8080


def test_request_equivalence():
    assert normalized_args('net.scan_ports', {'target':'LOCALHOST', 'ports':'80-81,80', 'rescan':True}) == normalized_args('net.scan_ports', {'target':'localhost', 'ports':'81,80'})
    assert normalized_args('web.discover_paths', {'url':'http://localhost'}) == normalized_args('web.discover_paths', {'url':'http://localhost:80/'})
    assert explicit_target('Fuzz http://localhost:8080/app') == ('localhost', 'http://localhost:8080/app')
