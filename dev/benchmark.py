#!/usr/bin/env python3
"""Run sequential, bounded Linux benchmarks and check generated-capture results."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = {
    'early': ['-r', '-q', '%:show:SPCHECK.*'],
    'late': ['-r', '-q', '%:exit:SPCHECK.*'],
    'multiple': ['-r', '-q', '%:show:SPCHECK.*', '-q', '%:exit:SPCHECK.*'],
    'repeated': ['-r', '-q', '+%:show:SPCHECK.*'],
    'no-match': ['-r', '-q', 'show:SPCHECK NEVER_PRESENT'],
    'notes': ['-n', '-q', '%:show:Image family:.*'],
}


def expected_output(files, workload, via_links=False):
    chunks = []
    for item in files:
        name = item['latest' if via_links else 'path']
        markers = {
            'early': [item['first_marker']],
            'late': [item['exit_marker']],
            'multiple': [item['first_marker'], item['exit_marker']],
            'repeated': item['show_markers'],
            'no-match': [],
        }[workload]
        chunks.extend(f'{name}:{marker}\n' for marker in markers)
    return ''.join(chunks).encode()


def measure(command, timeout, memory_mib, stdin_text, *, capture_output=True):
    def limits():
        resource.setrlimit(resource.RLIMIT_AS, (memory_mib * 1024 ** 2,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(timeout) + 1,) * 2)
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        started = time.monotonic()
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                   preexec_fn=limits, start_new_session=True)
        timed_out = False
        try:
            try:
                process.stdin.write(stdin_text.encode())
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
            while True:
                pid, status, usage = os.wait4(process.pid, os.WNOHANG)
                if pid:
                    break
                if time.monotonic() - started > timeout:
                    timed_out = True
                    os.killpg(process.pid, 9)
                    _, status, usage = os.wait4(process.pid, 0)
                    break
                time.sleep(0.02)
            process.returncode = os.waitstatus_to_exitcode(status)
        except BaseException:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, 9)
                except ProcessLookupError:
                    pass
                process.wait()
            raise
        elapsed = time.monotonic() - started
        stdout.seek(0)
        digest = hashlib.sha256()
        output_size = 0
        chunks = [] if capture_output else None
        while chunk := stdout.read(1024 * 1024):
            digest.update(chunk)
            output_size += len(chunk)
            if chunks is not None:
                chunks.append(chunk)
        output = b''.join(chunks) if chunks is not None else None
        stderr.seek(0)
        errors = stderr.read()
    return {
        'wall_seconds': round(elapsed, 4),
        'cpu_seconds': round(usage.ru_utime + usage.ru_stime, 4),
        'peak_rss_mib': round(usage.ru_maxrss / 1024, 2),
        'exit_status': process.returncode, 'timed_out': timed_out,
        'stdout_bytes': output_size, 'stdout_sha256': digest.hexdigest(),
        'stderr_tail': errors.decode(errors='replace')[-2000:],
    }, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', type=Path, help='Generated profile directory containing manifest.json')
    parser.add_argument('--script', type=Path, default=ROOT / 'showparse.py')
    parser.add_argument('--workload', action='append', choices=WORKLOADS)
    parser.add_argument('--limit-files', type=int)
    parser.add_argument('--via-links', action='store_true')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--memory-mib', type=int, default=3072)
    parser.add_argument('--output', type=Path, help='New JSON report path; existing reports are never overwritten')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('This resource-accounting harness targets Linux')
    if not 0 < args.timeout <= 3600 or not 128 <= args.memory_mib <= 4096:
        parser.error('Timeout must be 0–3600 seconds and memory must be 128–4096 MiB')
    if args.repeat < 1 or (args.limit_files is not None and args.limit_files < 1):
        parser.error('Repeat and file limit must be positive')
    dataset = args.dataset.resolve()
    manifest_bytes = (dataset / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest['format'] != 1:
        parser.error('Unsupported manifest format')
    files = sorted(manifest['files'], key=lambda item: item['path'])
    if args.limit_files:
        files = files[:args.limit_files]
    if not files:
        parser.error('Dataset contains no files')
    paths = []
    for item in files:
        path = dataset / item['latest' if args.via_links else 'path']
        if path.stat().st_size != item['bytes']:
            parser.error(f'Capture size differs from manifest: {path}')
        paths.append(str(path))
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output_path = args.output or ROOT / 'benchmark-results' / f'{manifest["profile"]}-{timestamp}.json'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        'format': 1, 'started_utc': timestamp, 'python': sys.version,
        'platform': platform.platform(), 'cpu_count': os.cpu_count(),
        'script': str(args.script.resolve()),
        'script_sha256': hashlib.sha256(args.script.read_bytes()).hexdigest(),
        'dataset': str(dataset), 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'files': len(files), 'bytes': sum(item['bytes'] for item in files),
        'via_links': args.via_links, 'timeout_seconds': args.timeout,
        'memory_limit_mib': args.memory_mib,
        'cache_policy': 'Uncontrolled OS cache; runs after generation may be warm. No cache eviction.',
        'complete': False, 'results': [],
    }
    # Create exclusively before running anything, then checkpoint this file after each workload.
    with output_path.open('x', encoding='utf-8') as destination:
        def checkpoint():
            destination.seek(0)
            json.dump(report, destination, indent=2)
            destination.write('\n')
            destination.truncate()
            destination.flush()
        checkpoint()
        for repetition in range(1, args.repeat + 1):
            for workload in args.workload or WORKLOADS:
                command = [sys.executable, '-I', str(args.script.resolve()), *WORKLOADS[workload], *paths]
                result, output = measure(command, args.timeout, args.memory_mib, '\n' * 3)
                if workload == 'notes':
                    text = output.decode(errors='replace')
                    groups = re.findall(r'matched (\d+) times?', text)
                    families = {item['vendor'] for item in files}
                    correct = (sum(map(int, groups)) == len(files) and len(groups) == len(families)
                               and all(f'Image family: synthetic-{vendor}' in text for vendor in families)
                               and text.count('Notes: ') == len(groups)
                               and 'NOTES REPORT' not in text)
                else:
                    expected = expected_output(files, workload, args.via_links)
                    correct = output == expected
                    result['expected_sha256'] = hashlib.sha256(expected).hexdigest()
                result.update(workload=workload, repetition=repetition,
                              correct=correct and result['exit_status'] == 0 and not result['timed_out'])
                report['results'].append(result)
                checkpoint()
                print(f'{workload} #{repetition}: {result["wall_seconds"]:.3f}s, '
                      f'{result["peak_rss_mib"]:.1f} MiB peak, '
                      f'{"PASS" if result["correct"] else "FAIL"}', flush=True)
        report['complete'] = True
        checkpoint()
    print(f'Report: {output_path}')
    return 0 if all(result['correct'] for result in report['results']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
