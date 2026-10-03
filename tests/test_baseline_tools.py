"""Check fixture integrity and benchmark guardrails without generating large data."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dev import generate_captures as fixtures
from dev.benchmark import measure

ROOT = Path(__file__).resolve().parents[1]


class BaselineTools(unittest.TestCase):
    def test_deterministic_sizes_hashes_links_and_nonshow_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, vendor in enumerate(fixtures.VENDORS):
                first = root / f'{vendor}.txt'
                record = fixtures.write_capture(first, 65536, index + 1, vendor)
                self.assertEqual(first.stat().st_size, 65536)
                self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), record['sha256'])
                self.assertEqual(first.with_suffix('.show').read_bytes(), first.read_bytes())
                copied = root / 'copy'
                copied.mkdir(exist_ok=True)
                second = copied / first.name
                fixtures.write_capture(second, 65536, index + 1, vendor)
                self.assertEqual(first.read_bytes(), second.read_bytes())
                result = subprocess.run([sys.executable, '-I', str(ROOT / 'showparse.py'), '-r',
                                         '-q', f'+%:{record["diagnostic_command"]}:SPCHECK.*', str(first)],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = ''.join(f'{first.name}:{marker}\n' for marker in record['diagnostic_markers'])
                self.assertEqual(result.stdout, expected)

    def test_budget_rejection_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'data'
            result = subprocess.run([sys.executable, str(ROOT / 'dev/generate_captures.py'),
                                     '--profile', 'full', '--output', str(destination),
                                     '--budget-gib', '1', '--plan'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('exceeds budget', result.stderr)
            self.assertFalse(destination.exists())

    def test_exclusive_capture_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'existing.txt'
            path.write_text('preserve me')
            with self.assertRaises(FileExistsError):
                fixtures.write_capture(path, 65536, 1, 'cisco')
            self.assertEqual(path.read_text(), 'preserve me')

    def test_timeout_and_memory_limit(self):
        result, _ = measure([sys.executable, '-c', 'import time; time.sleep(5)'], 0.1, 128, '')
        self.assertTrue(result['timed_out'])
        self.assertNotEqual(result['exit_status'], 0)
        result, _ = measure([sys.executable, '-c', 'bytearray(256 * 1024 ** 2)'], 5, 128, '')
        self.assertFalse(result['timed_out'])
        self.assertNotEqual(result['exit_status'], 0)
        self.assertIn('MemoryError', result['stderr_tail'])

    def test_benchmark_checks_smoke_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures.generate(root / 'smoke', [65536] * 3)
            output = root / 'results.json'
            result = subprocess.run([sys.executable, str(ROOT / 'dev/benchmark.py'), str(root / 'smoke'),
                                     '--via-links', '--output', str(output)], capture_output=True, text=True,
                                    timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text())
            self.assertTrue(report['complete'])
            self.assertEqual(len(report['results']), 6)
            self.assertTrue(all(item['correct'] for item in report['results']))

    def test_digest_only_measurement_checks_output_without_retaining_it(self):
        result, output = measure([sys.executable, '-c', 'print("test output")'],
                                 5, 128, '', capture_output=False)
        self.assertIsNone(output)
        self.assertEqual(result['exit_status'], 0)
        self.assertEqual(result['stdout_bytes'], len(b'test output\n'))
        self.assertEqual(result['stdout_sha256'], hashlib.sha256(b'test output\n').hexdigest())

    def test_selected_command_benchmark_verifies_every_workload(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'results.json'
            command = [sys.executable, str(ROOT / 'dev/benchmark_selected.py'), '--mib', '1',
                       '--output', str(output)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(output.read_text())
            self.assertEqual(len(report['results']), 8)
            self.assertTrue(all(item['verified'] for item in report['results']))
            before = output.read_bytes()
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
