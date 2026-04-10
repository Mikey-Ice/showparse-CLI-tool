#!/usr/bin/env python3
"""
Beginner starter template for consuming showparse_fsm JSON from stdin.

Use it like this:

    python3 showparse_fsm.py -q "show version" \
        --template tests/textfsm_templates/show_version_uptime.textfsm \
        test1/data/*.dat \
    | python3 showparse_fsm_json_helper_template.py

This script is intentionally simple. Edit the "EDIT HERE" section to build
whatever per-file analysis you want.
"""

import json
import sys


def main():
    data = json.load(sys.stdin)

    # EDIT HERE:
    # This example prints one line per parsed record.
    #
    # Three nested levels:
    #   1. file_entry -> one object per processed file
    #   2. match      -> one extracted command match inside that file
    #   3. record     -> one TextFSM record returned from that match
    #
    # Good first edits to try:
    #   - change record.get("UPTIME") to a different field
    #   - print the whole record instead of one field
    #   - collect rows into a list instead of printing immediately
    #   - remove the status filters if you want to inspect failures too
    for file_entry in data:
        # Skip file-level failures for the basic happy-path example.
        if file_entry["status"] not in {"ok", "partial_parse_error"}:
            continue

        for match in file_entry["matches"]:
            # Only process successfully parsed matches in this starter version.
            if match["status"] != "ok":
                continue

            # A match can return many records, especially for logs, interfaces,
            # ARP, MAC tables, and other list-style outputs.
            for record in match["records"]:
                print(
                    f'{file_entry["file"]}: '
                    f'command={match["matched_command"]} '
                    f'uptime={record.get("UPTIME")}'
                )


if __name__ == "__main__":
    main()
