#!/usr/bin/env python3
"""Bounded benchmarks for individual large commands, filtering, and formatting."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

from benchmark import measure

ROOT = Path(__file__).resolve().parents[1]
LOG = b'Oct 03 12:34:56 Router1 service: informational event with routine interface status normal.\n'
CONFIG = b'interface Ethernet1\n description uplink\n switchport mode trunk\n no shutdown\n!\n'


def repeated_digest(chunk, count):
    digest = hashlib.sha256()
    batch = chunk * 1024
    for _ in range(count // 1024):
        digest.update(batch)
    digest.update(chunk * (count % 1024))
    return len(chunk) * count, digest.hexdigest()


def write_repeated(stream, chunk, count):
    batch = chunk * 1024
    for _ in range(count // 1024):
        stream.write(batch)
    stream.write(chunk * (count % 1024))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--script', type=Path, default=ROOT / 'showparse.py')
    parser.add_argument('--mib', type=int, choices=(1, 8, 64, 128), default=64)
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 10:
        parser.error('--repeat must be between 1 and 10')
    # Reserve the result path before generating data or running measurements.
    with args.output.open('x') as report:
        results = []
        with tempfile.TemporaryDirectory(prefix='showparse-selected-') as directory:
            capture = Path(directory) / 'router.log'
            log_count = args.mib * 1024**2 // len(LOG)
            block_count = args.mib * 1024**2 // len(CONFIG)
            with capture.open('wb') as stream:
                stream.write(b'R1#show logging\n')
                write_repeated(stream, LOG, log_count)
                stream.write(b'R1#show run\n')
                write_repeated(stream, CONFIG, block_count)
                stream.write(b'R1#\n')
            small = lambda text: (len(text), hashlib.sha256(text).hexdigest())
            count_output = f'router.log:{log_count}\n'.encode()
            workloads = [
                ('count', ['-q', '#:show logging'], small(count_output)),
                ('filter_miss', ['-q', 'show logging:NEVER_PRESENT'], small(b'')),
                ('filtered_count', ['-q', '#:show logging:normal'], small(count_output)),
                ('multiple_counts', ['-q', '#:show logging', '-Q', '#@:show run:shutdown'],
                 small(count_output + f'router.log:{block_count * 2}\n'.encode())),
                ('block_count', ['-Q', '#:show run:shutdown'], small(f'router.log:{block_count * 4}\n'.encode())),
                ('selective_trimmed_count', ['-Q', '#@%:show run:shutdown'], small(f'router.log:{block_count * 2}\n'.encode())),
                ('block_miss', ['-Q', 'show run:NEVER_PRESENT'], small(b'')),
                ('raw_full', ['-q', 'show logging'], repeated_digest(b'router.log:' + LOG, log_count)),
            ]
            for iteration in range(args.repeat):
                for name, queries, expected in workloads:
                    result, _ = measure([sys.executable, '-I', str(args.script.resolve()), '-r', *queries,
                                         str(capture)], 60, 1024, '', capture_output=False)
                    result.update(workload=name, iteration=iteration + 1)
                    result['verified'] = (result['exit_status'] == 0 and
                                          (result['stdout_bytes'], result['stdout_sha256']) == expected)
                    results.append(result)
                    print(f"{name}: {result['wall_seconds']:.3f}s, {result['peak_rss_mib']:.1f} MiB, verified={result['verified']}", flush=True)
        json.dump({'script_sha256': hashlib.sha256(args.script.read_bytes()).hexdigest(),
                   'command_mib': args.mib, 'results': results}, report, indent=2)
        report.write('\n')
    return int(not all(result['verified'] for result in results))


if __name__ == '__main__':
    sys.exit(main())
