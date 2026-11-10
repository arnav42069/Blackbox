"""Opt-in integration verification of CLI Ctrl+C/--stop and owned browser cleanup."""
import json
from pathlib import Path
import signal
import socket
import subprocess
import time
import sys
from scripts.qa_support import write_json

ROOT = Path(__file__).resolve().parents[1]


def listening(port):
    with socket.socket() as sock:
        return sock.connect_ex(('127.0.0.1', port)) == 0


def main():
    results = []
    for method in ('SIGINT', '--stop'):
        if listening(8876):
            raise RuntimeError('Shutdown validation requires free localhost port 8876')
        process = subprocess.Popen([str(ROOT / 'scripts/qa-loop.sh'), '--report-only', '--test-port', '8876'], cwd=ROOT, stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 60
            while not listening(8876):
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('QA browser fixture did not start before shutdown test')
                time.sleep(.1)
            if method == 'SIGINT':
                process.send_signal(signal.SIGINT)
            else:
                subprocess.run([str(ROOT / 'scripts/qa-loop.sh'), '--stop'], cwd=ROOT, check=True, capture_output=True)
            output, _ = process.communicate(timeout=20)
            for _ in range(50):
                if not listening(8876): break
                time.sleep(.1)
            latest = json.loads((ROOT / 'qa-reports/latest.json').read_text())
            report = ROOT / latest['report']
            data = json.loads(report.read_text())
            journal = report.parent / 'cycle-0/browser-tool-journal.json'
            passed = process.returncode == 130 and not listening(8876) and data['status'] == 'stopped' and journal.is_file()
            results.append({'method': method, 'exit_code': process.returncode, 'passed': passed, 'fixture_port_closed': not listening(8876), 'browser_journal_retained': journal.is_file(), 'report': latest['report']})
            if not passed:
                raise AssertionError(results[-1])
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=10)
            write_json(ROOT / 'qa-reports/shutdown-validation.json', results)
    print('Ctrl+C and --stop both preserved reports, exited 130, and closed the owned browser fixture port.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
