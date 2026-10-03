"""Terminal shutdown must not turn successful parsing into Python diagnostics."""
import contextlib
import io
import os
import subprocess
import sys
from unittest import mock

import showparse
from test_showparse import CaptureCase, SCRIPT


class CliLifecycle(CaptureCase):
    def closed_pipe(self, *arguments, files=None):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            return subprocess.run(
                [sys.executable, '-I', str(SCRIPT), *arguments,
                 *map(str, files if files is not None else [self.capture])],
                stdout=write_fd, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                text=True, timeout=10,
            )
        finally:
            os.close(write_fd)

    def test_small_output_closed_at_final_flush_is_quiet(self):
        for mode in (('-r',), ('--no-banner',), ('--no-color',), ('-n',)):
            with self.subTest(mode=mode):
                result = self.closed_pipe(*mode, '-q', 'show version')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('BrokenPipe', result.stderr)
                self.assertNotIn('Exception', result.stderr)

    def test_large_output_closed_during_write_is_quiet(self):
        self.capture.write_text('R1#show logging\n' + 'normal log event\n' * 10000 + 'R1#\n')
        result = self.closed_pipe('-r', '-q', 'show logging')
        self.assertEqual((result.returncode, result.stderr), (0, ''))

    def test_help_closed_at_final_flush_is_quiet(self):
        result = self.closed_pipe('--help', files=[])
        self.assertEqual((result.returncode, result.stderr), (0, ''))

    def test_broken_recovery_output_does_not_hide_a_report_save_failure(self):
        class ClosedReport(io.StringIO):
            def write(self, text):
                if 'NOTES REPORT' in text:
                    raise BrokenPipeError('reader closed')
                return super().write(text)

        report = self.root / 'notes.txt'
        with mock.patch.object(sys, 'argv', ['showparse', '-n', '-o', str(report),
                                             '-q', 'show version', str(self.capture)]), \
                mock.patch.object(showparse, 'collect_notes', return_value=[('capture.txt', 'reviewed')]), \
                mock.patch.object(showparse, 'save_notes_report', side_effect=OSError('save failed')), \
                contextlib.redirect_stdout(ClosedReport()), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as failure:
                showparse.main()
        self.assertEqual(failure.exception.code, 1)
        self.assertIn('save failed', stderr.getvalue())
        self.assertFalse(report.exists())

    def test_final_flush_keeps_a_capture_read_failure_status(self):
        unreadable = self.root / 'z-directory'
        unreadable.mkdir()
        result = self.closed_pipe('-r', '-q', 'show version', files=[self.capture, unreadable])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Error reading file', result.stderr)
        self.assertNotIn('BrokenPipe', result.stderr)

    def test_interrupt_during_notes_preparation_never_collects_or_saves(self):
        report = self.root / 'notes.txt'
        with mock.patch.object(sys, 'argv', ['showparse', '-n', '-o', str(report),
                                             '-q', 'show version', str(self.capture)]), \
                mock.patch.object(showparse, 'get_query_results', side_effect=KeyboardInterrupt), \
                mock.patch.object(showparse, 'collect_notes') as collect, \
                mock.patch.object(showparse, 'save_notes_report') as save, \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(showparse.main(), 130)
        collect.assert_not_called()
        save.assert_not_called()
        self.assertFalse(report.exists())
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), '\n')
