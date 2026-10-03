"""CLI behavior baseline and regression checks for repaired defects."""
import io
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import showparse

SCRIPT = Path(__file__).resolve().parents[1] / 'showparse.py'
CAPTURE = '''Login banner
R1#show version
Network OS release 17
Model: LAB-100
R1#show running-config
hostname R1
!
interface Ethernet1
 description WAN
 no shutdown
!
interface Ethernet2
 description SPARE
 shutdown
!
R1#show version
Network OS release 18
Model: LAB-200
R1#request support information
Support bundle complete
R1#exit
Connection closed
'''


class CaptureCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='showparse-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.capture = self.root / 'capture.txt'
        self.capture.write_text(CAPTURE, encoding='utf-8')

    def run_cli(self, *args, files=None, input_text=''):
        return subprocess.run([sys.executable, '-I', str(SCRIPT), *args,
                               *map(str, files if files is not None else [self.capture])],
                              input=input_text, capture_output=True, text=True, timeout=10)

    def query(self, text, flag='-q'):
        result = self.run_cli('--no-banner', flag, text)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout


class BehaviorBaseline(CaptureCase):
    def test_first_literal_case_insensitive_prefix(self):
        self.assertEqual(self.query('SHOW VER'), 'Network OS release 17\nModel: LAB-100\n')

    def test_arbitrary_command_and_prompt_styles(self):
        for prompt in ('R1#', 'admin@junos>', 'admin@panos>', 'shell$', 'cli%'):
            with self.subTest(prompt=prompt):
                self.capture.write_text(f'{prompt} request support information\nComplete\n{prompt}\n')
                self.assertEqual(self.query('request support'), 'Complete\n')

    def test_all_matches_and_rendered_count(self):
        self.assertEqual(self.query('+%:show version:release \\d+'), 'release 17\n\nrelease 18\n')
        self.assertEqual(self.query('#+~:show version'), '6\n')

    def test_case_sensitive_pattern(self):
        self.assertEqual(self.query('show version:model'), 'Model: LAB-100\n')
        self.assertEqual(self.query('/:show version:model'), '\n')

    def test_full_and_selective_blocks(self):
        self.assertEqual(self.query('show run:^ shutdown$', '-Q'),
                         'interface Ethernet2\n description SPARE\n shutdown\n')
        self.assertEqual(self.query('@:show run:^ shutdown$', '-Q'),
                         'interface Ethernet2\n shutdown\n')
        self.assertEqual(self.query('@%:show run:^ shutdown$', '-Q'),
                         'interface Ethernet2\n shutdown\n')

    def test_parent_match_keeps_children(self):
        self.assertEqual(self.query('@:show run:^interface Ethernet1$', '-Q'),
                         'interface Ethernet1\n description WAN\n no shutdown\n')

    def test_mixed_query_order_and_raw_output(self):
        result = self.run_cli('-r', '-q', 'show version:Model', '-Q', '@:show run:^ shutdown$',
                              '-q', 'request support')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'capture.txt:Model: LAB-100\n'
                         'capture.txt:interface Ethernet2\ncapture.txt: shutdown\n'
                         'capture.txt:Support bundle complete\n')

    def test_invalid_utf8_crlf_and_unicode(self):
        self.capture.write_bytes(b'R1#show version\r\nBad byte: \xff\r\n'
                                 + 'Location: Montréal\r\nR1#\r\n'.encode())
        self.assertEqual(self.query('show version'), 'Bad byte: \ufffd\nLocation: Montréal\n')

    def test_indented_history_and_metadata_do_not_match(self):
        self.capture.write_text('!Command: show version\n    R1#show version\nFAKE\n' + CAPTURE)
        self.assertEqual(self.query('show version:Model'), 'Model: LAB-100\n')

    def test_symlink_and_dat_extension(self):
        path = self.root / 'device.dat'
        self.capture.rename(path)
        link = self.root / 'device.show'
        link.symlink_to(path.name)
        result = self.run_cli('-r', '-q', 'show version:Model', files=[link])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'device.show:Model: LAB-100\n')

    def test_and_filter_rejects_nonmatching_pattern(self):
        result = self.run_cli('-r', '-A', '-q', 'show version:Model', '-q', 'show version:NEVER')
        self.assertEqual((result.returncode, result.stdout), (0, ''))

    def test_notes_grouping_and_saved_report(self):
        other = self.root / 'second.txt'
        other.write_text(CAPTURE)
        report = self.root / 'notes.txt'
        result = self.run_cli('-n', '-q', 'show version', '-o', str(report),
                              files=[self.capture, other], input_text='reviewed\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('matched 2 times', result.stdout)
        self.assertEqual(result.stdout.count('Notes: '), 1)
        self.assertIn('(2/2)', result.stderr)
        self.assertIn('capture.txt:reviewed\nsecond.txt:reviewed', report.read_text())

    def test_incompatible_modes_are_rejected(self):
        self.assertEqual(self.run_cli('-n', '-r', '-q', 'show version').returncode, 2)
        self.assertEqual(self.run_cli('-o', str(self.root / 'notes'), '-q', 'show version').returncode, 2)

    def test_first_command_does_not_fall_through_after_filter_miss(self):
        self.assertEqual(self.query('show version:release 18'), '\n')
        self.assertEqual(self.query('+:show version:release 18'), 'Network OS release 18\n')

    def test_last_output_and_empty_last_command_without_newline(self):
        self.capture.write_text('R1#show version\nlast output')
        self.assertEqual(self.query('show version'), 'last output\n')
        self.capture.write_text('R1#show version')
        self.assertEqual(self.query('~:show version'), 'show version\n')


class FileProcessing(CaptureCase):
    def test_multiple_queries_open_capture_once(self):
        queries = [showparse.parse_q_query(text) for text in
                   ('request support', 'show version:Model', '+%:show version:release \\d+')]
        with mock.patch('builtins.open', wraps=open) as opened:
            results = showparse.get_query_results(self.capture, queries)
        self.assertEqual([result.output for result in results],
                         ['Support bundle complete', 'Model: LAB-100', 'release 17\n\nrelease 18'])
        self.assertTrue(all(result.status is showparse.QueryStatus.MATCH for result in results))
        self.assertEqual(opened.call_count, 1)

    def test_read_failure_is_shared_without_reopening(self):
        queries = [showparse.parse_q_query(text) for text in ('show version', 'show run')]
        with mock.patch('builtins.open', side_effect=OSError('synthetic failure')) as opened:
            results = showparse.get_query_results(self.capture, queries)
        self.assertEqual(results, [showparse.QueryResult(showparse.QueryStatus.READ_ERROR,
                                                       error='synthetic failure')] * 2)
        self.assertEqual(opened.call_count, 1)


class TrackingReader(io.StringIO):
    """Count consumed text and reject unbounded reads in streaming tests."""
    consumed = 0

    def read(self, size=-1):
        if size < 0:
            raise AssertionError('Streaming must not load the entire capture')
        result = super().read(size)
        self.consumed += len(result)
        return result

    def readline(self, size=-1):
        result = super().readline(size)
        self.consumed += len(result)
        return result


class StreamingBehavior(CaptureCase):
    def tracked_queries(self, content, queries):
        reader = TrackingReader(content)
        specs = [showparse.parse_q_query(query) for query in queries]
        with mock.patch('builtins.open', return_value=reader) as opened:
            results = showparse.get_query_results(self.capture, specs)
        self.assertEqual(opened.call_count, 1)
        self.assertTrue(reader.closed)
        return [result.output for result in results], reader.consumed

    def test_stops_after_all_first_matches_in_any_query_order(self):
        content = ('R1#show run\nconfig\nR1#show logging\nlog entry\nR1#unused\n'
                   + 'unneeded output\n' * 100000)
        results, consumed = self.tracked_queries(content, ['show logging', 'show run'])
        self.assertEqual(results, ['log entry', 'config'])
        self.assertLess(consumed, len(content) // 10)

    def test_filter_miss_still_stops_at_first_command(self):
        content = 'R1#show version\nold\nR1#unused\n' + 'padding\n' * 100000
        content += 'R1#show version\nnew\nR1#\n'
        results, consumed = self.tracked_queries(content, ['show version:new'])
        self.assertEqual(results, [''])
        self.assertLess(consumed, len(content) // 10)

    def test_missing_and_plus_queries_reach_end(self):
        content = 'R1#show version\nfirst\nR1#unused\n' + 'padding\n' * 20000
        content += 'R1#show version\nlast'
        for queries, expected in [(['missing'], ['']),
                                  (['+:show version'], ['first\n\nlast'])]:
            with self.subTest(queries=queries):
                results, consumed = self.tracked_queries(content, queries)
                self.assertEqual(results, expected)
                self.assertEqual(consumed, len(content))

    def test_shared_blocks_keep_filters_and_first_all_modes_independent(self):
        results, _ = self.tracked_queries(CAPTURE, ['show version:release 18',
                                                  '+%:show version:release \\d+', 'show version:Model'])
        self.assertEqual(results, ['', 'release 17\n\nrelease 18', 'Model: LAB-100'])

    def test_overlapping_prompts_preserve_command_encounter_order(self):
        content = 'R1#show version\nA\nR2#show version\nB\nR2#done\nC\nR1#\n'
        results, _ = self.tracked_queries(content, ['+:show version'])
        self.assertEqual(results, ['A\nR2#show version\nB\nR2#done\nC\n\nB'])

    def test_prompt_and_encoding_across_chunk_boundaries(self):
        self.capture.write_bytes(b'Login banner\r' + b'x' * 65534 + b'\r\n'
                                 + 'R1#show version\r\nLocation: Montréal\r\n'.encode()
                                 + b'Bad byte: \xff\r\nR1#request support\r\nComplete\r\nR1#')
        queries = [showparse.parse_q_query(command) for command in ('request support', 'show version')]
        for chunk_size in (1, 7, 65536):
            with self.subTest(chunk_size=chunk_size), mock.patch.object(showparse, 'CAPTURE_CHUNK_SIZE', chunk_size):
                self.assertEqual([result.output for result in showparse.get_query_results(self.capture, queries)],
                                 ['Complete', 'Location: Montréal\nBad byte: \ufffd'])

    def test_multiline_prefix_retains_buffered_compatibility(self):
        queries = [showparse.parse_q_query('show version\nNetwork OS')]
        self.assertEqual(showparse.get_query_results(self.capture, queries),
                         [showparse.QueryResult(showparse.QueryStatus.MATCH, output='Model: LAB-100')])

    def test_midstream_read_error_does_not_return_partial_success(self):
        class FailingReader(TrackingReader):
            def read(self, size=-1):
                if self.consumed:
                    raise OSError('interrupted read')
                return super().read(size)
        reader = FailingReader('R1#show version\nfirst\nR1#\n' + 'padding\n' * 20000)
        queries = [showparse.parse_q_query(command) for command in ('+:show version', 'missing')]
        with mock.patch('builtins.open', return_value=reader):
            self.assertEqual(showparse.get_query_results(self.capture, queries),
                             [showparse.QueryResult(showparse.QueryStatus.READ_ERROR,
                                                    error='interrupted read')] * 2)


class QueryOutcomes(CaptureCase):
    def test_missing_command_does_not_pass_and(self):
        for mode in (('-r',), ('--no-color',), ('--no-banner',)):
            with self.subTest(mode=mode):
                result = self.run_cli(*mode, '-A', '-q', 'show version', '-q', 'missing command')
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_and_success_preserves_normal_and_raw_output(self):
        for mode in (('-r',), ('--no-color',), ('--no-banner',)):
            with self.subTest(mode=mode):
                queries = ('-q', 'show version:Model', '-Q', '@:show run:^ shutdown$')
                expected = self.run_cli(*mode, *queries)
                result = self.run_cli(*mode, '-A', *queries)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (0, expected.stdout, ''))
                self.assertIn('Model: LAB-100', result.stdout)
                self.assertIn('interface Ethernet2', result.stdout)

    def test_no_match_is_empty_content_without_a_diagnostic(self):
        for query in ('missing command', 'show version:NEVER', '+~#:missing command'):
            with self.subTest(query=query):
                result = self.run_cli('-r', '-q', query, '-q', 'show version:Model')
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (0, 'capture.txt:Model: LAB-100\n', ''))

    def test_status_reflects_selected_content_and_modifiers(self):
        self.capture.write_text('R1#empty\nR1#zero\n0\nR1#show version\nold\n'
                                'R1#show version\nnew\nR1#')
        cases = [
            ('empty', showparse.QueryStatus.NO_MATCH, ''),
            ('#:empty', showparse.QueryStatus.NO_MATCH, ''),
            ('~:empty', showparse.QueryStatus.MATCH, 'empty'),
            ('#~:empty', showparse.QueryStatus.MATCH, '1'),
            ('zero', showparse.QueryStatus.MATCH, '0'),
            ('+~:missing', showparse.QueryStatus.NO_MATCH, ''),
            ('~:show version:NEVER', showparse.QueryStatus.NO_MATCH, ''),
            ('show version:new', showparse.QueryStatus.NO_MATCH, ''),
            ('+:show version:new', showparse.QueryStatus.MATCH, 'new'),
            ('#+:show version:new', showparse.QueryStatus.MATCH, '1'),
            ('+:show version:NEVER', showparse.QueryStatus.NO_MATCH, ''),
            ('%:show version:^', showparse.QueryStatus.NO_MATCH, ''),
        ]
        for query, status, output in cases:
            with self.subTest(query=query):
                spec = showparse.parse_q_query(query)
                expected = showparse.QueryResult(status, output=output)
                self.assertEqual(showparse.process_query(self.capture, spec), expected)
                self.assertEqual(showparse.process_query(self.capture, spec, content=self.capture.read_text()),
                                 expected)
                cli = self.run_cli('-r', '-A', '-q', query)
                self.assertEqual((cli.returncode, cli.stderr), (0, ''))
                self.assertEqual(cli.stdout, f'capture.txt:{output}\n' if output else '')

    def test_whitespace_only_matched_text_does_not_satisfy_and(self):
        for query in ('%:show version:[ ]+', '%:show version:$', '#%:show version:[ ]+'):
            with self.subTest(query=query):
                selected = showparse.process_query(self.capture, showparse.parse_q_query(query))
                self.assertIs(selected.status, showparse.QueryStatus.NO_MATCH)
                result = self.run_cli('-r', '-A', '-q', query)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_error_like_capture_text_is_still_a_match(self):
        text = "Error reading file: device message\nCommand 'show logging' not found in file."
        self.capture.write_text(f'R1#show logging\n{text}\nR1#')
        result = self.run_cli('--no-banner', '-A', '-q', 'show logging')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, text + '\n', ''))

    def test_multiline_compatibility_uses_the_same_outcomes_and_single_read(self):
        specs = [showparse.parse_q_query(text) for text in
                 ('show version\nNetwork OS', 'missing\ncommand', 'show version:NEVER')]
        with mock.patch('builtins.open', wraps=open) as opened:
            results = showparse.get_query_results(self.capture, specs)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual([result.status for result in results],
                         [showparse.QueryStatus.MATCH, showparse.QueryStatus.NO_MATCH,
                          showparse.QueryStatus.NO_MATCH])
        with mock.patch('builtins.open', side_effect=PermissionError('access denied')):
            results = showparse.get_query_results(self.capture, specs)
        self.assertEqual(results, [showparse.QueryResult(showparse.QueryStatus.READ_ERROR,
                                                       error='access denied')] * 3)

    def test_notes_excludes_failed_and_file(self):
        for flag, query in (('-q', 'missing command'), ('-q', 'show version:NEVER'),
                            ('-Q', 'show run:NEVER')):
            with self.subTest(query=query):
                result = self.run_cli('-n', '-A', '-q', 'show version', flag, query,
                                      input_text='reviewed\n')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('Notes: ', result.stdout)
                self.assertNotIn('Unique Output', result.stdout)
                self.assertNotIn('capture.txt:reviewed', result.stdout)

    def test_notes_without_and_keeps_empty_results_for_annotation(self):
        result = self.run_cli('-n', '-q', 'missing command', input_text='absent\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('(empty)', result.stdout)
        self.assertIn('capture.txt:absent', result.stdout)
        self.assertNotIn('not found in file', result.stdout)

    def test_notes_and_only_groups_eligible_files(self):
        other = self.root / 'rejected.txt'
        other.write_text('R1#show version\nNetwork OS release 17\nModel: LAB-100\nR1#')
        result = self.run_cli('-n', '-A', '-q', 'show version:Model', '-Q', 'show run:shutdown',
                              files=[self.capture, other], input_text='reviewed\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('Notes: '), 1)
        self.assertIn('matched 1 time', result.stdout)
        self.assertIn('capture.txt:reviewed', result.stdout)
        self.assertNotIn('rejected.txt', result.stdout)

    def test_read_failure_is_diagnosed_once_and_never_becomes_output(self):
        for mode in (('-r',), ('--no-color',), ('--no-banner',), ('-n',)):
            for and_flag in ((), ('-A',)):
                with self.subTest(mode=mode, and_flag=and_flag):
                    result = self.run_cli(*mode, *and_flag, '-q', 'show version', '-q', 'show run',
                                          files=[self.root], input_text='reviewed\n')
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stderr.count('Error reading file'), 1)
                    self.assertIn(str(self.root), result.stderr)
                    self.assertNotIn('Error reading file', result.stdout)
                    self.assertNotIn('Notes: ', result.stdout)
                    if '-n' not in mode:
                        self.assertEqual(result.stdout, '')

    def test_read_failure_continues_other_files_and_preserves_failure_status(self):
        for name in ('a-unreadable', 'z-unreadable'):
            unreadable = self.root / name
            unreadable.mkdir()
            for mode in (('-r',), ('--no-banner',)):
                with self.subTest(name=name, mode=mode):
                    result = self.run_cli(*mode, '-A', '-q', 'show version:Model',
                                          files=[unreadable, self.capture])
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stderr.count('Error reading file'), 1)
                    expected = 'capture.txt:Model: LAB-100\n' if '-r' in mode else 'Model: LAB-100\n'
                    self.assertEqual(result.stdout, expected)

    def test_notes_can_save_valid_results_after_read_failure(self):
        report = self.root / 'report.txt'
        result = self.run_cli('-n', '-A', '-q', 'show version:Model', '-o', str(report),
                              files=[self.root, self.capture], input_text='reviewed\n')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.count('Error reading file'), 1)
        self.assertEqual(result.stdout.count('Notes: '), 1)
        self.assertIn('matched 1 time', result.stdout)
        self.assertIn('capture.txt:reviewed', report.read_text())
        self.assertNotIn('Error reading file', report.read_text())


class QueryArguments(CaptureCase):
    def test_invalid_regex_is_a_usage_error(self):
        result = self.run_cli('-r', '-q', 'show version:[')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn("Invalid pattern in query 'show version:['", result.stderr)
        self.assertIn('position 0', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_attached_query_is_not_silently_lost(self):
        result = self.run_cli('-r', '-qshow version:Model')
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertEqual(result.stdout, 'capture.txt:Model: LAB-100\n')

    def test_supported_query_forms_produce_identical_results(self):
        for short, long, query, expected in (
            ('-q', '--query', 'show version:Model', 'capture.txt:Model: LAB-100\n'),
            ('-Q', '--query-block', '@:show run:^ shutdown$',
             'capture.txt:interface Ethernet2\ncapture.txt: shutdown\n'),
        ):
            forms = [(short, query), (short + query,), (short + '=' + query,),
                     (long, query), (long + '=' + query,)]
            if short == '-Q':
                forms.append(('--query-b=' + query,))  # Existing unambiguous argparse abbreviation.
            for arguments in forms:
                with self.subTest(arguments=arguments):
                    result = self.run_cli('-r', *arguments)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, ''))

    def test_mixed_forms_and_clustered_flags_preserve_query_order(self):
        result = self.run_cli('-rAqshow version:Model', '--query-block=@:show run:^ shutdown$',
                              '--query', 'request support', '-q=+:show version:release 18')
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertEqual(result.stdout, 'capture.txt:Model: LAB-100\n'
                         'capture.txt:interface Ethernet2\ncapture.txt: shutdown\n'
                         'capture.txt:Support bundle complete\ncapture.txt:Network OS release 18\n')

    def test_invalid_patterns_fail_even_with_empty_or_missing_inputs(self):
        self.capture.write_text('')
        for flag in ('-q', '-Q'):
            for pattern in ('[', '(', '*', '(?L)x', '(?au)x', 'x{4294967296}', '(' * 1000):
                with self.subTest(flag=flag, pattern=pattern[:20]):
                    result = self.run_cli('-r', flag, 'missing command:' + pattern)
                    self.assertEqual((result.returncode, result.stdout), (2, ''))
                    self.assertIn('Invalid pattern in query', result.stderr)
                    self.assertNotIn('Traceback', result.stderr)
        result = self.run_cli('-r', '-q', 'show version:[', files=[self.root / 'missing.txt'])
        self.assertEqual(result.returncode, 2)
        self.assertIn('Invalid pattern', result.stderr)
        self.assertNotIn('No files', result.stderr)

    def test_invalid_later_query_prevents_all_input_and_report_access(self):
        report = self.root / 'report.txt'
        report.write_text('existing report')
        for mode in (('-r',), ('-n', '-o', str(report))):
            for invalid in (('-Q', 'show run:['), ('-Q', '+::shutdown'), ('-q', '%:show version')):
                with self.subTest(mode=mode, invalid=invalid):
                    argv = [str(SCRIPT), *mode, '-q', 'show version:Model', *invalid, str(self.capture)]
                    with mock.patch.object(sys, 'argv', argv), \
                            mock.patch('builtins.open') as opened, \
                            mock.patch.object(showparse, 'expand_file_patterns') as expanded, \
                            mock.patch.object(sys, 'stdout', new_callable=io.StringIO) as stdout, \
                            mock.patch.object(sys, 'stderr', new_callable=io.StringIO) as stderr:
                        with self.assertRaises(SystemExit) as stopped:
                            showparse.main()
                        self.assertEqual(stopped.exception.code, 2)
                        opened.assert_not_called()
                        expanded.assert_not_called()
                        self.assertEqual(stdout.getvalue(), '')
                        self.assertNotIn('Traceback', stderr.getvalue())
        self.assertEqual(report.read_text(), 'existing report')
        self.assertEqual(self.capture.read_text(), CAPTURE)

    def test_malformed_queries_have_specific_usage_errors(self):
        cases = [('-q', '', 'Empty query'), ('-Q', '   ', 'Empty query'),
                 ('-q', '+:', 'Missing command'), ('-Q', '+::shutdown', 'Missing command'),
                 ('-q', '%:show version', 'require a grep pattern'),
                 ('-Q', '@:show run', 'require a grep pattern'),
                 ('-q', '/:show version', 'require a grep pattern'),
                 ('-q', '@:show run:shutdown', 'only supported with -Q'),
                 ('-Q', '?:show run:shutdown', 'Unknown modifier'),
                 ('-Q', ':shutdown', 'Invalid modifier syntax')]
        for flag, query, message in cases:
            with self.subTest(flag=flag, query=query):
                result = self.run_cli('-r', flag, query)
                self.assertEqual((result.returncode, result.stdout), (2, ''))
                self.assertIn(message, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
        for arguments, files in [(('-q',), []), (('-Q',), []), (('-r',), None)]:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_cli(*arguments, files=files).returncode, 2)

    def test_option_like_filenames_after_double_dash_are_only_files(self):
        filenames = ['-q', 'show run']
        for filename in filenames:
            (self.root / filename).write_text(CAPTURE)
        result = subprocess.run([sys.executable, '-I', str(SCRIPT), '-r', '-q',
                                 'show version:Model', '--', *filenames], cwd=self.root,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, '-q:Model: LAB-100\nshow run:Model: LAB-100\n', ''))

    def test_notes_summary_retains_attached_and_abbreviated_queries(self):
        result = self.run_cli('-nAqshow version:Model', '--query-b=@:show run:^ shutdown$',
                              '--query=request support', input_text='reviewed\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = next(line.removeprefix('Command: ') for line in result.stdout.splitlines()
                       if line.startswith('Command: '))
        tokens = shlex.split(summary)
        self.assertEqual(tokens, ['showparse', '-A', '-q', 'show version:Model',
                                  '-Q', '@:show run:^ shutdown$', '-q', 'request support'])
        replay = self.run_cli('-r', *tokens[1:])
        self.assertEqual((replay.returncode, replay.stderr), (0, ''))
        self.assertEqual(replay.stdout, 'capture.txt:Model: LAB-100\n'
                         'capture.txt:interface Ethernet2\ncapture.txt: shutdown\n'
                         'capture.txt:Support bundle complete\n')

    def test_valid_regex_features_and_flags_keep_their_meaning(self):
        for query, expected in [
            ('show version:Model: LAB-\\d+', 'Model: LAB-100\n'),
            ('%:show version:(?<=LAB-)\\d+', '100\n'),
            ('%:show version:(?P<model>LAB-\\d+)', 'LAB-100\n'),
            ('show version:(?-i:model)', '\n'),
            ('/:show version:(?i)model', 'Model: LAB-100\n'),
        ]:
            with self.subTest(query=query):
                self.assertEqual(self.query(query), expected)

    def test_optional_patterns_and_literal_command_prefixes_are_preserved(self):
        expected = self.run_cli('-r', '-q', 'show run').stdout
        for flag, query in (('-Q', 'show run'), ('-q', 'show run:   ')):
            with self.subTest(flag=flag, query=query):
                result = self.run_cli('-r', flag, query)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, ''))
        self.capture.write_text('R1#request [support]\nComplete\nR1#')
        self.assertEqual(self.query('request ['), 'Complete\n')


class NotesCaptureProtection(CaptureCase):
    def test_notes_cannot_overwrite_capture(self):
        result = self.run_cli('-n', '-q', 'show version', '-o', str(self.capture), input_text='reviewed\n')
        self.assertEqual(self.capture.read_text(), CAPTURE)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
