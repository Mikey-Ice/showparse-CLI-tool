"""Streaming filters must preserve rendering semantics while bounding working memory."""
import itertools
import random
import tracemalloc
from unittest import mock

import showparse
from test_showparse import CaptureCase


class SelectedOutput(CaptureCase):
    def compare_buffered(self, content, queries):
        self.capture.write_text(content, encoding='utf-8')
        expected = [showparse.process_query(self.capture, query, content=content) for query in queries]
        for size in (1, 7, 65536):
            with self.subTest(chunk_size=size), mock.patch.object(showparse, 'CAPTURE_CHUNK_SIZE', size):
                self.assertEqual(showparse.get_query_results(self.capture, queries), expected)

    def test_all_modifier_combinations_share_first_and_repeated_blocks(self):
        content = ('R1#show run\n \t\n  interface EDGE \n child EDGE \n\n\n!'
                   '\ninterface core\n shutdown  \n \t\nR1#show run all\n'
                   'interface EDGE\n child two\nR1#\n')
        queries = []
        for use_blocks in (False, True):
            options = '+~#%/' + ('@' if use_blocks else '')
            for flags in itertools.product((False, True), repeat=len(options)):
                mods = frozenset(option for option, enabled in zip(options, flags) if enabled)
                for pattern in ('EDGE|shutdown', '^', r'\s+$'):
                    queries.append(showparse.QuerySpec('show run', pattern, use_blocks, mods))
        self.compare_buffered(content, queries)

    def test_stripping_happens_before_matching_first_and_last_lines(self):
        content = 'R1#show run\n\t \n  first   \n \n middle  \n\t\n last   \n \t\nR1#'
        queries = [showparse.parse_q_query(text) for text in
                   ('show run', '#:show run', 'show run:^first   $', 'show run:^ last$',
                    'show run:^ last   $', 'show run:^ middle  $', '+~%:show run:^',
                    '+#~%:show run:^', 'show run:^ $')]
        self.compare_buffered(content, queries)

    def test_overlapping_prompts_keep_text_order_and_add_counts(self):
        content = 'R1#show run\nA\u2028B\nR2#show run\nC\x1cD\nR2#\nE\nR1#\n'
        queries = [showparse.parse_q_query(text) for text in
                   ('+:show run', '+#:show run', '+#~:show run', '+~:show run', '+%:show run:^')]
        self.compare_buffered(content, queries)

    def test_one_empty_regex_selection_differs_from_multiple_empty_selections(self):
        self.capture.write_text('R1#show run\none\nR1#show run\none\n\n\ntwo\nR1#')
        queries = [showparse.parse_q_query(text) for text in
                   ('~%:show run:^', '+#~%:show run:^', '+~%:show run:^')]
        results = showparse.get_query_results(self.capture, queries)
        self.assertEqual([result.output for result in results], ['', '1', 'show run\n\n\n\n'])

    def test_seeded_whitespace_and_config_boundaries_match_buffered_path(self):
        randomizer = random.Random(31005)
        lines = ['', ' ', '\t ', '!', '  ! ', 'parent EDGE ', ' child core', ' shutdown ',
                 'R1#show run', 'R2#show run', 'R1#', 'R2#', 'metadata']
        for _ in range(20):
            content = '\n'.join(randomizer.choices(lines, k=40))
            queries = []
            for _ in range(12):
                blocks = randomizer.choice((False, True))
                mods = frozenset(option for option in '+~#%@' if randomizer.randrange(2))
                queries.append(showparse.QuerySpec('show', randomizer.choice((None, '^', r'\s+', 'edge')), blocks, mods))
            self.compare_buffered(content, queries)

    def test_large_filtered_and_counted_command_does_not_retain_its_text(self):
        line = 'Normal operational event with an interface status message.\n'
        lines = 40000
        with self.capture.open('w') as capture:
            capture.write('R1#show logging\n')
            for _ in range(lines // 1000):
                capture.write(line * 1000)
            capture.write('R1#\n')
        queries = [showparse.parse_q_query(query) for query in
                   ('#:show logging', '#:show logging:Normal', 'show logging:NEVER')]
        tracemalloc.start()
        try:
            results = showparse.get_query_results(self.capture, queries)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual([result.output for result in results], [str(lines), str(lines), ''])
        self.assertLess(peak, 1024 * 1024, f'Unexpected selected-text retention: {peak} bytes')

    def test_large_blank_runs_do_not_change_counts_or_last_line_trimming(self):
        self.capture.write_text('R1#show run\nfirst \n' + '\t\n' * 10000
                                + 'last \n' + ' \n' * 10000 + 'R1#\n')
        queries = [showparse.parse_q_query(query) for query in
                   ('#:show run', 'show run:^last$', '%:show run:^\t$')]
        results = showparse.get_query_results(self.capture, queries)
        self.assertEqual([result.output for result in results],
                         ['2', 'last', '\n'.join(['\t'] * 10000)])

    def test_raw_and_normal_iterators_keep_exact_buffered_format(self):
        outputs = [' first\n\n second \n', '', 'A\u2028B']
        self.assertEqual(''.join(showparse.iter_raw_output('device', outputs)),
                         'device: first\ndevice: second \ndevice:A\u2028B\n')
        self.assertEqual(''.join(showparse.iter_normal_output('device', outputs, False)),
                         '\n\n----------------------------------------\n\n'.join(outputs) + '\n')
