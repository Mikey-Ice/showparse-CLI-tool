"""Quiet annotation controls, literal notes, and explicit cancellation."""
import io
import os
import pty
import select
import signal
import subprocess
import sys
import time
from unittest import mock

import showparse
from test_notes_reports import TerminalText
from test_showparse import CAPTURE, SCRIPT, CaptureCase


class NotesControls(CaptureCase):
    def setUp(self):
        super().setUp()
        self.second = self.root / 'second.txt'
        self.second.write_text(CAPTURE.replace('Model: LAB-100', 'Model: LAB-200'))
        self.third = self.root / 'third.txt'
        self.third.write_text(CAPTURE.replace('Model: LAB-100', 'Model: LAB-300'))

    def test_plain_q_and_Q_are_notes(self):
        result = self.run_cli('-n', '-q', 'show version:Model', files=[self.capture, self.second],
                              input_text='q\nQ\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('capture.txt:q\nsecond.txt:Q\n', result.stdout)
        self.assertEqual(result.stdout.count('Notes: '), 2)

    def test_successful_file_save_has_only_the_group_output_and_plain_prompt(self):
        report = self.root / 'report.txt'
        result = self.run_cli('-n', '-q', 'show version:Model', '-o', str(report), input_text='reviewed\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r'\A\n-+<<< Unique Output 1 of 1 \| matched 1 time >>>-+\n\n'
                                       r'Model: LAB-100\nNotes: \Z')
        self.assertIn('capture.txt:reviewed', report.read_text())
        self.assertEqual(result.stderr.strip(), '(1/1)')

    def test_done_saves_completed_groups_and_leaves_remaining_groups_unannotated(self):
        copy = self.root / 'copy.txt'
        copy.write_text(CAPTURE)
        report = self.root / 'report.txt'
        result = self.run_cli('-n', '-q', 'show version:Model', '-o', str(report),
                              files=[self.capture, copy, self.second, self.third],
                              input_text='reviewed\n/done\nunused\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('Notes: '), 2)
        self.assertIn('Unique Output 1 of 3 | matched 2 times', result.stdout)
        self.assertNotIn('Unique Output 3', result.stdout)
        self.assertTrue(result.stdout.endswith('Notes: '))
        self.assertIn('capture.txt:reviewed\ncopy.txt:reviewed\n', report.read_text())
        for unwanted in ('second.txt:', 'third.txt:', '/done', 'unused'):
            self.assertNotIn(unwanted, report.read_text())

    def test_done_without_output_file_prints_the_report_without_a_completion_message(self):
        result = self.run_cli('-n', '-q', 'show version:Model', files=[self.capture, self.second, self.third],
                              input_text='reviewed\n/done\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('NOTES REPORT', result.stdout)
        self.assertIn('capture.txt:reviewed\n', result.stdout)
        self.assertNotIn('second.txt:', result.stdout)
        self.assertNotIn('Finished', result.stdout)
        self.assertNotIn('saved', result.stdout)
        self.assertTrue(result.stdout.endswith('capture.txt:reviewed\n\n'))

    def test_skipped_groups_and_empty_sessions_are_quiet(self):
        report = self.root / 'report.txt'
        for answers in ('\n', '/done\n', ''):
            for output_args in ((), ('-o', str(report))):
                with self.subTest(answers=answers, output_args=output_args):
                    result = self.run_cli('-n', '-q', 'show version:Model', *output_args, input_text=answers)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout.endswith('Notes: '))
                    self.assertNotIn('NOTES REPORT', result.stdout)
                    self.assertNotIn('No notes', result.stdout)
                    self.assertNotIn('/help', result.stdout)
                    self.assertFalse(report.exists())
        result = self.run_cli('-n', '-q', 'show version:Model',
                              files=[self.capture, self.second, self.third], input_text='\nreviewed\n\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('second.txt:reviewed\n', result.stdout)
        self.assertNotIn('capture.txt:', result.stdout)
        self.assertNotIn('third.txt:', result.stdout)

    def test_help_is_on_request_and_keeps_the_current_group(self):
        result = self.run_cli('-n', '-q', 'show version:Model', input_text='/help\n/HELP\nreviewed\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('Unique Output'), 1)
        self.assertEqual(result.stdout.count('Model: LAB-100'), 1)
        self.assertEqual(result.stdout.count('Notes: '), 3)
        self.assertEqual(result.stdout.count('Notes controls:'), 2)
        self.assertIn('capture.txt:reviewed\n', result.stdout)
        self.assertNotIn('capture.txt:/help', result.stdout)
        cli_help = self.run_cli('--help', files=[])
        self.assertEqual(cli_help.returncode, 0, cli_help.stderr)
        for controls in (result.stdout, cli_help.stdout):
            for expected in ('/done', '/help', 'Ctrl-C', '//text', 'q/Q are text',
                             'end of input', 'discard notes', 'saves quietly'):
                self.assertIn(expected, controls)
        self.assertIn(showparse.NOTES_HELP, cli_help.stdout)

    def test_unknown_slash_commands_do_not_become_notes_or_advance_the_group(self):
        result = self.run_cli('-n', '-q', 'show version:Model',
                              input_text='/dnoe\n/quit\n/done extra\nreviewed\n')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count('Unique Output'), 1)
        self.assertEqual(result.stdout.count('Notes: '), 4)
        self.assertEqual(result.stderr.count('Unknown notes command'), 3)
        self.assertIn('capture.txt:reviewed\n', result.stdout)
        self.assertNotIn('capture.txt:/', result.stdout)

    def test_double_slash_removes_exactly_one_slash_and_preserves_note_text(self):
        cases = [('//done', '/done'), ('//help', '/help'), ('//etc/example', '/etc/example'),
                 ('///server/share', '//server/share'), ('//', '/'),
                 ('  //Mixed Case  ', '  /Mixed Case  '),
                 ('Review /done later', 'Review /done later'), ('\\Q', '\\Q')]
        for entered, expected in cases:
            with self.subTest(entered=entered):
                result = self.run_cli('-n', '-q', 'show version:Model', input_text=entered + '\n')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('capture.txt:' + expected + '\n', result.stdout)
                self.assertEqual(result.stdout.count('Notes: '), 1)
                self.assertNotIn('Unknown notes command', result.stderr)

    def test_commands_are_case_insensitive_whole_lines_with_optional_outer_spaces(self):
        result = self.run_cli('-n', '-q', 'show version:Model',
                              input_text='  /HeLp  \n  /DoNe  \nignored\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('Notes controls:'), 1)
        self.assertEqual(result.stdout.count('Notes: '), 2)
        self.assertNotIn('NOTES REPORT', result.stdout)

    def test_end_of_input_finishes_with_completed_notes(self):
        report = self.root / 'report.txt'
        result = self.run_cli('-n', '-q', 'show version:Model', '-o', str(report),
                              files=[self.capture, self.second], input_text='reviewed\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('capture.txt:reviewed\n', report.read_text())
        self.assertNotIn('second.txt:', report.read_text())
        self.assertTrue(result.stdout.endswith('Notes: '))

    def test_cancellation_discards_completed_notes_and_never_calls_the_save_function(self):
        report = self.root / 'report.txt'
        for destination in ('stdout', 'new', 'existing'):
            if destination == 'existing':
                report.write_text('previous report')
            argv = [str(SCRIPT), '-n', '-q', 'show version:Model']
            if destination != 'stdout':
                argv.extend(('-o', str(report)))
            argv.extend((str(self.capture), str(self.second)))
            answers = (['yes'] if destination == 'existing' else []) + ['reviewed', KeyboardInterrupt()]
            with self.subTest(destination=destination), \
                    mock.patch.object(sys, 'argv', argv), \
                    mock.patch.object(sys, 'stdin', TerminalText()), \
                    mock.patch.object(sys, 'stdout', new_callable=io.StringIO) as output, \
                    mock.patch.object(sys, 'stderr', new_callable=TerminalText) as errors, \
                    mock.patch('builtins.input', side_effect=answers), \
                    mock.patch.object(showparse, 'save_notes_report') as save:
                self.assertEqual(showparse.main(), 130)
                save.assert_not_called()
                self.assertNotIn('NOTES REPORT', output.getvalue())
                self.assertNotIn('Canceled', output.getvalue() + errors.getvalue())
                self.assertNotIn('for recovery', errors.getvalue())
            if destination == 'existing':
                self.assertEqual(report.read_text(), 'previous report')
            else:
                self.assertFalse(report.exists())
        self.assertEqual(list(self.root.glob('.showparse-notes-*.tmp')), [])

    def test_terminal_interrupt_after_a_completed_note_keeps_the_approved_report_untouched(self):
        report = self.root / 'report.txt'
        report.write_text('previous report')
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen([sys.executable, '-I', str(SCRIPT), '-n', '-q',
                                        'show version:Model', '-o', str(report),
                                        str(self.capture), str(self.second)],
                                       stdin=slave, stdout=slave, stderr=slave)
            transcript = bytearray()

            def read_until(marker, count=1):
                deadline = time.monotonic() + 5
                while transcript.count(marker) < count:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                        self.fail(f'Terminal did not show {marker!r}: {bytes(transcript)!r}')
                    transcript.extend(os.read(master, 65536))

            try:
                read_until(b'[y/N]')
                os.write(master, b'y\n')
                read_until(b'Notes: ')
                os.write(master, b'reviewed\n')
                read_until(b'Notes: ', count=2)
                os.kill(process.pid, signal.SIGINT)
                self.assertEqual(process.wait(timeout=5), 130)
                while select.select([master], [], [], 0)[0]:
                    transcript.extend(os.read(master, 65536))
                self.assertEqual(report.read_text(), 'previous report')
                for unwanted in (b'Canceled', b'Traceback', b'NOTES REPORT', b'Notes report saved', b'Notes controls:'):
                    self.assertNotIn(unwanted, transcript)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        finally:
            os.close(master)
            os.close(slave)

    def test_done_does_not_mask_a_capture_read_error(self):
        result = self.run_cli('-n', '-q', 'show version:Model', files=[self.root, self.capture], input_text='/done\n')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Error reading file', result.stderr)
        self.assertNotIn('NOTES REPORT', result.stdout)
