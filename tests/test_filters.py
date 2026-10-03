"""Configuration selection contracts independent of capture extraction."""
import re
import unittest

import showparse


CONFIG = ('  orphan before parent\n!\nhostname R1\ninterface Ethernet1\n'
          ' description EDGE\n shutdown\n\ninterface Ethernet2\n'
          ' description core\n no shutdown\n  !  \n orphan after separator\n'
          'router ospf 1\n network 10.0.0.0\n')
FIRST = 'interface Ethernet1\n description EDGE\n shutdown'
SECOND = 'interface Ethernet2\n description core\n no shutdown'


class ConfigFilters(unittest.TestCase):
    def test_line_filters_preserve_regex_features_and_first_match_per_line(self):
        output = 'Name: EDGE 12 34\nname: core 56\nblank\n'
        cases = [('(?i)NAME', 0), (r'(\d+)', re.IGNORECASE),
                 (r'(?<=: )\w+', re.IGNORECASE), ('^', re.IGNORECASE),
                 ('', re.IGNORECASE), ('Name', 0)]
        for pattern, flags in cases:
            with self.subTest(pattern=pattern, flags=flags):
                matches = [(line, re.search(pattern, line, flags)) for line in output.split('\n')]
                self.assertEqual(showparse.grep_output(output, pattern, flags),
                                 '\n'.join(line for line, match in matches if match))
                self.assertEqual(showparse.grep_output_matches_only(output, pattern, flags),
                                 '\n'.join(match.group(0) for _, match in matches if match))

    def test_full_blocks_keep_order_and_do_not_duplicate_multiple_matches(self):
        self.assertEqual(showparse.grep_output_with_blocks(CONFIG, 'interface|shutdown'),
                         FIRST + '\n\n' + SECOND)

    def test_parent_matches_select_all_children_in_both_modes(self):
        for selective in (False, True):
            self.assertEqual(showparse.grep_output_with_blocks_selective(
                CONFIG, 'Ethernet1', matched_children_only=selective), FIRST)

    def test_selective_children_keep_parent_and_matching_children(self):
        self.assertEqual(showparse.grep_output_with_blocks_selective(
            CONFIG, 'shutdown', matched_children_only=True),
            'interface Ethernet1\n shutdown\n\ninterface Ethernet2\n no shutdown')

    def test_trim_keeps_unmatched_context_and_trims_selected_matches(self):
        self.assertEqual(showparse.grep_output_with_blocks_selective(
            CONFIG, 'EDGE|shutdown', trim_matches=True),
            'interface Ethernet1\nEDGE\nshutdown\n\ninterface Ethernet2\n description core\nshutdown')
        self.assertEqual(showparse.grep_output_with_blocks_selective(
            CONFIG, 'Ethernet1|EDGE', matched_children_only=True, trim_matches=True),
            'Ethernet1\nEDGE\n shutdown')

    def test_orphans_and_separators_are_never_blocks(self):
        self.assertEqual(showparse.grep_output_with_blocks(CONFIG, 'orphan|^!'), '')
        self.assertEqual(showparse.grep_output_with_blocks(CONFIG, 'hostname'), 'hostname R1')
        self.assertEqual(showparse.grep_output_with_blocks(CONFIG, 'network'),
                         'router ospf 1\n network 10.0.0.0')

    def test_case_flags_and_zero_width_matches(self):
        self.assertEqual(showparse.grep_output_with_blocks(CONFIG, 'edge', regex_flags=0), '')
        self.assertEqual(showparse.grep_output_with_blocks(CONFIG, 'edge', re.IGNORECASE), FIRST)
        self.assertEqual(showparse.grep_output_with_blocks_selective(
            'parent\n child', '^', trim_matches=True), '\n')
