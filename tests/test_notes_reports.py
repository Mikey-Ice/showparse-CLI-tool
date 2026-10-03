"""Protect captures and completed reports across interactive notes saves."""
import io
import os
import pty
import select
import subprocess
import sys
import time
from unittest import mock

import showparse
from test_showparse import CAPTURE, SCRIPT, CaptureCase


class TerminalText(io.StringIO):
    def isatty(self):
        return True


class NotesReports(CaptureCase):
    def invoke_notes(self, destination, answers, *, files=None):
        argv = [str(SCRIPT), '-n', '-q', 'show version:Model', '-o', str(destination),
                *map(str, files if files is not None else [self.capture])]
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(sys, 'stdin', TerminalText()), \
                mock.patch.object(sys, 'stdout', new_callable=io.StringIO) as output, \
                mock.patch.object(sys, 'stderr', new_callable=TerminalText) as errors, \
                mock.patch('builtins.input', side_effect=answers) as asked:
            status = showparse.main()
        return status, output.getvalue(), errors.getvalue(), asked

    def assert_no_temporary_reports(self):
        self.assertEqual(list(self.root.rglob('.showparse-notes-*.tmp')), [])

    def test_input_aliases_are_rejected_before_confirmation_or_parsing(self):
        symlink = self.root / 'latest.show'
        symlink.symlink_to(self.capture.name)
        hardlink = self.root / 'capture-alias.txt'
        os.link(self.capture, hardlink)
        directory_link = self.root / 'alias'
        directory_link.symlink_to(self.root, target_is_directory=True)
        for destination, files in [(self.capture, [self.capture]), (symlink, [self.capture]),
                                   (hardlink, [self.capture]), (self.capture, [symlink]),
                                   (directory_link / self.capture.name, [self.capture])]:
            with self.subTest(destination=destination, files=files), \
                    mock.patch.object(showparse, 'get_query_results') as read:
                status, output, errors, asked = self.invoke_notes(destination, ['yes', 'reviewed'], files=files)
                self.assertEqual(status, 1)
                self.assertIn('input capture', errors)
                self.assertEqual(output, '')
                asked.assert_not_called()
                read.assert_not_called()
                self.assertEqual(self.capture.read_text(), CAPTURE)

    def test_same_basename_in_a_different_directory_is_allowed(self):
        folder = self.root / 'reports'
        folder.mkdir()
        report = folder / self.capture.name
        status, _, errors, asked = self.invoke_notes(report, ['reviewed'])
        self.assertEqual(status, 0, errors)
        self.assertEqual(asked.call_count, 1)
        self.assertIn('capture.txt:reviewed', report.read_text())
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.capture.read_text(), CAPTURE)
        self.assert_no_temporary_reports()

    def test_existing_report_is_refused_with_piped_input(self):
        report = self.root / 'report.txt'
        report.write_text('previous report')
        result = self.run_cli('-n', '-q', 'show version', '-o', str(report), input_text='y\nreviewed\n')
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertIn('requires a terminal', result.stderr)
        self.assertNotIn('Replace it', result.stderr)
        self.assertEqual(report.read_text(), 'previous report')

    def test_existing_report_in_input_glob_is_always_protected(self):
        report = self.root / 'report.txt'
        report.write_text('previous report')
        result = self.run_cli('-n', '-q', 'show version', '-o', str(report),
                              files=[self.root / '*.txt'], input_text='y\nreviewed\n')
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertIn('input capture', result.stderr)
        self.assertEqual(report.read_text(), 'previous report')

    def test_unverifiable_input_identity_stops_before_confirmation(self):
        report = self.root / 'report.txt'
        report.write_text('previous report')
        real_stat = os.stat

        def deny_capture(path, *args, **kwargs):
            if str(path) == str(self.capture):
                raise PermissionError('cannot inspect capture')
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(showparse.os, 'stat', side_effect=deny_capture), \
                mock.patch.object(showparse, 'get_query_results') as read:
            status, output, errors, asked = self.invoke_notes(report, ['y'])
        self.assertEqual((status, output), (1, ''))
        self.assertIn('Cannot verify report protection', errors)
        asked.assert_not_called()
        read.assert_not_called()
        self.assertEqual(report.read_text(), 'previous report')

    def test_symlink_parent_and_dotdot_keep_filesystem_path_meaning(self):
        actual = self.root / 'actual'
        child = actual / 'child'
        child.mkdir(parents=True)
        link = self.root / 'shortcut'
        link.symlink_to(child, target_is_directory=True)
        status, _, errors, _ = self.invoke_notes(link / '..' / 'report.txt', ['reviewed'])
        self.assertEqual(status, 0, errors)
        self.assertIn('capture.txt:reviewed', (actual / 'report.txt').read_text())
        self.assertFalse((self.root / 'report.txt').exists())

    def test_unsupported_platform_is_rejected_before_collecting_notes(self):
        with mock.patch.object(showparse.os, 'name', 'nt'), \
                mock.patch.object(showparse, 'get_query_results') as read:
            status, output, errors, asked = self.invoke_notes(self.root / 'report.txt', [])
        self.assertEqual((status, output), (1, ''))
        self.assertIn('use -n without -o', errors)
        asked.assert_not_called()
        read.assert_not_called()

    def test_decline_eof_and_interrupt_do_not_start_parsing(self):
        report = self.root / 'report.txt'
        report.write_text('previous report')
        for answer, expected_status in [('', 1), ('N', 1), ('no', 1), (EOFError(), 1), (KeyboardInterrupt(), 130)]:
            with self.subTest(answer=answer), mock.patch.object(showparse, 'get_query_results') as read:
                status, output, errors, asked = self.invoke_notes(report, [answer])
                self.assertEqual(status, expected_status)
                self.assertEqual(output, '')
                self.assertIn(str(report), errors)
                self.assertIn('[y/N]', errors)
                asked.assert_called_once_with()
                read.assert_not_called()
                self.assertEqual(report.read_text(), 'previous report')

    def test_confirmation_accepts_only_yes_and_preserves_old_report_until_save(self):
        report = self.root / 'report.txt'
        report.write_text('previous report')
        report.chmod(0o640)
        answers = iter(['maybe', ' YES ', 'reviewed café'])

        def respond(prompt=''):
            self.assertEqual(report.read_text(), 'previous report')
            return next(answers)

        status, output, errors, asked = self.invoke_notes(report, respond)
        self.assertEqual(status, 0, errors)
        self.assertEqual(asked.call_count, 3)
        self.assertIn('Please answer y or n', errors)
        self.assertNotIn('Notes report saved', output)
        self.assertIn('capture.txt:reviewed café', report.read_text(encoding='utf-8'))
        self.assertEqual(report.stat().st_mode & 0o777, 0o640)
        self.assert_no_temporary_reports()

    def test_real_terminal_confirmation_precedes_notes_and_does_not_truncate(self):
        report = self.root / 'report.txt'
        report.write_text('previous report')
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen([sys.executable, '-I', str(SCRIPT), '-n', '-q',
                                        'show version:Model', '-o', str(report), str(self.capture)],
                                       stdin=slave, stdout=slave, stderr=slave)
            transcript = bytearray()

            def read_until(marker):
                deadline = time.monotonic() + 5
                while marker not in transcript:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                        self.fail(f'Terminal did not show {marker!r}: {bytes(transcript)!r}')
                    transcript.extend(os.read(master, 65536))

            try:
                read_until(b'[y/N]')
                self.assertNotIn(b'Unique Output', transcript)
                self.assertEqual(report.read_text(), 'previous report')
                os.write(master, b'y\n')
                read_until(b'Notes: ')
                self.assertEqual(report.read_text(), 'previous report')
                os.write(master, b'reviewed\n')
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertIn('capture.txt:reviewed', report.read_text())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        finally:
            os.close(master)
            os.close(slave)

    def test_no_notes_leaves_existing_report_unchanged_or_new_path_absent(self):
        report = self.root / 'report.txt'
        for exists in (False, True):
            if exists:
                report.write_text('previous report')
            for answer in ('', '/done', EOFError()):
                with self.subTest(exists=exists, answer=answer):
                    status, output, _, _ = self.invoke_notes(report, (['yes'] if exists else []) + [answer])
                    self.assertEqual(status, 0)
                    self.assertNotIn('No notes collected', output)
                    self.assertNotIn('NOTES REPORT', output)
                    if exists:
                        self.assertEqual(report.read_text(), 'previous report')
                    else:
                        self.assertFalse(report.exists())
        self.assert_no_temporary_reports()

    def test_existing_report_symlink_preserves_link_and_updates_its_target(self):
        target = self.root / 'old-report.txt'
        target.write_text('previous report')
        link = self.root / 'report.txt'
        link.symlink_to(target.name)
        status, _, errors, _ = self.invoke_notes(link, ['y', 'reviewed'])
        self.assertEqual(status, 0, errors)
        self.assertIn(str(target), errors)
        self.assertTrue(link.is_symlink())
        self.assertIn('capture.txt:reviewed', target.read_text())

    def test_invalid_destinations_are_rejected_before_parsing(self):
        dangling = self.root / 'dangling'
        dangling.symlink_to('missing-target')
        loop = self.root / 'loop'
        loop.symlink_to(loop.name)
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        for destination in (self.root, dangling, loop, fifo, self.root / 'missing' / 'report.txt'):
            with self.subTest(destination=destination), mock.patch.object(showparse, 'get_query_results') as read:
                status, output, _, asked = self.invoke_notes(destination, [])
                self.assertEqual((status, output), (1, ''))
                asked.assert_not_called()
                read.assert_not_called()

    def test_existing_report_changed_during_notes_is_not_replaced(self):
        report = self.root / 'report.txt'
        for change in ('edit', 'replace', 'remove', 'capture-symlink', 'capture-hardlink'):
            report.write_text('previous report')

            def respond(prompt=''):
                if not prompt:
                    return 'y'
                if change == 'edit':
                    report.write_text('updated by someone else')
                else:
                    report.unlink()
                    if change == 'replace':
                        report.write_text('previous report')
                    elif change == 'capture-symlink':
                        report.symlink_to(self.capture)
                    elif change == 'capture-hardlink':
                        os.link(self.capture, report)
                return 'do not lose these notes'

            with self.subTest(change=change):
                status, output, errors, _ = self.invoke_notes(report, respond)
                self.assertEqual(status, 1)
                self.assertIn('for recovery', errors)
                self.assertIn('capture.txt:do not lose these notes', output)
                self.assertNotIn('Notes report saved', output)
                self.assertEqual(self.capture.read_text(), CAPTURE)
                if change == 'remove':
                    self.assertFalse(report.exists())
                else:
                    expected = CAPTURE if change.startswith('capture-') else (
                        'updated by someone else' if change == 'edit' else 'previous report')
                    self.assertEqual(report.read_text(), expected)
                    report.unlink()
        self.assert_no_temporary_reports()

    def test_new_destination_created_during_notes_is_not_replaced(self):
        report = self.root / 'report.txt'

        def respond(prompt):
            report.write_text('created by another process')
            return 'reviewed'

        status, output, errors, asked = self.invoke_notes(report, respond)
        self.assertEqual(status, 1)
        self.assertEqual(asked.call_count, 1)
        self.assertIn('changed during this run', errors)
        self.assertIn('capture.txt:reviewed', output)
        self.assertEqual(report.read_text(), 'created by another process')

    def test_input_symlink_becoming_the_report_is_detected_at_save(self):
        report = self.root / 'report.txt'
        report.write_text('previous report')
        latest = self.root / 'latest.show'
        latest.symlink_to(self.capture)

        def respond(prompt=''):
            if not prompt:
                return 'yes'
            latest.unlink()
            latest.symlink_to(report)
            return 'reviewed'

        status, output, errors, _ = self.invoke_notes(report, respond, files=[latest])
        self.assertEqual(status, 1)
        self.assertIn('input capture', errors)
        self.assertIn('latest.show:reviewed', output)
        self.assertEqual(report.read_text(), 'previous report')

    def test_symlink_retargeted_while_confirming_is_rejected_before_parsing(self):
        original = self.root / 'original.txt'
        original.write_text('previous report')
        other = self.root / 'other.txt'
        other.write_text('other report')
        link = self.root / 'report.txt'
        link.symlink_to(original)

        def respond():
            link.unlink()
            link.symlink_to(other)
            return 'y'

        with mock.patch.object(showparse, 'get_query_results') as read:
            status, _, errors, _ = self.invoke_notes(link, respond)
            self.assertEqual(status, 1)
            self.assertIn('changed during this run', errors)
            read.assert_not_called()
        self.assertEqual(original.read_text(), 'previous report')
        self.assertEqual(other.read_text(), 'other report')

    def test_parent_symlink_change_during_write_keeps_both_locations_untouched(self):
        original = self.root / 'original'
        other = self.root / 'other'
        original.mkdir()
        other.mkdir()
        (original / 'report.txt').write_text('previous report')
        (other / 'report.txt').write_text('other report')
        link = self.root / 'reports'
        link.symlink_to(original, target_is_directory=True)

        def change_parent(fd):
            link.unlink()
            link.symlink_to(other, target_is_directory=True)

        with mock.patch.object(showparse.os, 'fsync', side_effect=change_parent):
            status, output, errors, _ = self.invoke_notes(link / 'report.txt', ['y', 'reviewed'])
        self.assertEqual(status, 1)
        self.assertIn('changed during this run', errors)
        self.assertIn('capture.txt:reviewed', output)
        self.assertEqual((original / 'report.txt').read_text(), 'previous report')
        self.assertEqual((other / 'report.txt').read_text(), 'other report')
        self.assert_no_temporary_reports()

    def test_write_and_publication_failures_preserve_previous_report_and_notes(self):
        report = self.root / 'report.txt'
        for operation, exists in [('fsync', False), ('fsync', True), ('link', False), ('replace', True)]:
            if exists:
                report.write_text('previous report')
            with self.subTest(operation=operation, exists=exists), \
                    mock.patch.object(showparse.os, operation, side_effect=OSError('simulated save failure')):
                status, output, errors, _ = self.invoke_notes(report, (['y'] if exists else []) + ['reviewed'])
                self.assertEqual(status, 1)
                self.assertIn('simulated save failure', errors)
                self.assertIn('capture.txt:reviewed', output)
                if exists:
                    self.assertEqual(report.read_text(), 'previous report')
                    report.unlink()
                else:
                    self.assertFalse(report.exists())
                self.assert_no_temporary_reports()

    def test_new_destination_race_at_publication_never_overwrites(self):
        report = self.root / 'report.txt'
        real_link = os.link

        def competing_link(*args, **kwargs):
            report.write_text('concurrent report')
            return real_link(*args, **kwargs)

        with mock.patch.object(showparse.os, 'link', side_effect=competing_link):
            status, output, _, _ = self.invoke_notes(report, ['reviewed'])
        self.assertEqual(status, 1)
        self.assertEqual(report.read_text(), 'concurrent report')
        self.assertIn('capture.txt:reviewed', output)
        self.assert_no_temporary_reports()

    def test_interrupted_save_preserves_old_report_and_prints_collected_notes(self):
        report = self.root / 'report.txt'
        report.write_text('previous report')
        with mock.patch.object(showparse.os, 'fsync', side_effect=KeyboardInterrupt):
            status, output, errors, _ = self.invoke_notes(report, ['y', 'reviewed'])
        self.assertEqual(status, 130)
        self.assertIn('Save interrupted', errors)
        self.assertIn('capture.txt:reviewed', output)
        self.assertEqual(report.read_text(), 'previous report')
        self.assert_no_temporary_reports()
