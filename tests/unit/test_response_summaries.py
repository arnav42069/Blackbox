from blackbox.core.responses import evidence_summary


def test_unsupported_model_timing_is_removed_but_observed_utc_is_kept():
    from blackbox.core.responses import grounded_timing
    tools=[{'observed_at_utc':'2026-11-10T01:30:00+00:00'}]
    draft='HTTP 200\nTimestamp: 2025-09-19 14:06:34 UTC\n| Elapsed time | 6 ms |\nPage title: Fixture\n'
    assert grounded_timing(draft,tools)=='HTTP 200\nPage title: Fixture\n'
    assert grounded_timing('Timestamp: 2026-11-10T01:30:00+00:00',tools)=='Timestamp: 2026-11-10T01:30:00+00:00'
    assert grounded_timing('HTTP 200 was observed at http://127.0.0.1/. No vulnerability is verified.',tools)=='HTTP 200 was observed at http://127.0.0.1/. No vulnerability is verified.'
    assert grounded_timing('- `elapsed_ms: 7`\n',tools)==''


def test_summary_shows_measured_status_and_headers_without_inventing_findings():
    text=evidence_summary([{'url':'http://127.0.0.1:8080/','status':403,'headers':'HTTP/1.1 403 Forbidden\r\nServer: fixture\r\nContent-Type: text/plain\r\n'}],include_headers=True)
    assert 'HTTP 403' in text and 'fixture' in text and 'text/plain' in text
    assert 'vulnerable' not in text and 'HTTP 200' not in text


def test_unknown_service_is_not_presented_as_http():
    text=evidence_summary([{'kind':'port','target':'127.0.0.1','port':8443,'protocol':'tcp','state':'open','service':'unknown'}])
    assert 'open unknown' in text and '8443/tcp' in text
    assert 'https://' not in text


def test_import_summary_keeps_freshness_and_failure_explicit():
    text=evidence_summary([{'id':'scan-evidence','tool':'net.scan_ports','status':'failed','freshness':'stale','findings':[]}])
    assert 'failed' in text and 'stale' in text and 'scan-evidence' in text


def test_host_summary_keeps_unknown_fields_explicit():
    from blackbox.core.responses import host_summary
    text=host_summary({'tool_id':'measured-id','timestamp':123,'facts':{'os':'Linux','kernel':'fixture-kernel','cpu':None,'memory':None,'gpus':None}})
    assert 'fixture-kernel' in text and 'measured-id' in text and '123' in text
    assert 'Not measured' in text and 'NVIDIA' not in text
