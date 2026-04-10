#!/usr/bin/env python3
"""
showparse_fsm - Apply a TextFSM template to prompt-delimited command output.

This companion keeps showparse's bulk file workflow and prompt-aware extraction
rules, but emits machine-friendly JSON rooted by filename instead of human-
oriented banners and text output.
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys
from dataclasses import dataclass


DEFINED_Q_MODIFIERS = {"+"}
DEFINED_Q_BLOCK_MODIFIERS = {"+"}


@dataclass(frozen=True)
class QuerySpec:
    """Parsed query definition for one -q or -Q flag."""

    command: str
    grep_pattern: str | None
    use_blocks: bool = False
    modifiers: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CommandMatch:
    """One prompt-delimited command match extracted from a file."""

    header: str
    output: str


@dataclass(frozen=True)
class RenderedMatch:
    """One extracted command match plus the final text fed to TextFSM."""

    matched_command: str
    rendered_output: str


def _read_file_content(file_path):
    """Read and return file content, or an error tuple on failure."""
    try:
        with open(file_path, "r") as file_handle:
            return file_handle.read()
    except IOError as error:
        return (None, f"Error reading file: {error}")


def _get_command_pattern(command):
    """Return the prompt-delimited regex used for command extraction."""
    return rf"^(\S*[#>$%])( ?{re.escape(command)}[^\n]*)"


def _build_command_match(content, match):
    """Build a CommandMatch from one prompt-delimited regex match."""
    prompt = match.group(1)
    command_line = match.group(2)
    command_header = prompt + command_line

    output_start = match.end()
    end_pattern = rf"^{re.escape(prompt)}"
    end_match = re.search(end_pattern, content[output_start:], re.MULTILINE)

    if end_match:
        command_output = content[output_start : output_start + end_match.start()].strip()
    else:
        command_output = content[output_start:].strip()

    return CommandMatch(header=command_header, output=command_output)


def _render_matched_command_header(command_header):
    """Return the matched command line without the device prompt token."""
    return re.sub(r"^\S*[#>$%] ?", "", command_header, count=1)


def extract_commands(file_path, command):
    """
    Extract all outputs for commands matching a prefix from a .dat file.

    The prompt is any non-whitespace string immediately preceding the command,
    or separated from it by exactly one space, at the start of a line.
    """
    content = _read_file_content(file_path)
    if isinstance(content, tuple):
        return content

    pattern = _get_command_pattern(command)
    matches = list(re.finditer(pattern, content, re.MULTILINE | re.IGNORECASE))

    if not matches:
        return (None, f"Command '{command}' not found in file.")

    return [_build_command_match(content, match) for match in matches]


def extract_command(file_path, command):
    """
    Extract the first output for a command prefix from a .dat file.

    Returns tuple of (command_header, command_output) or (None, error_message)
    if not found.
    """
    content = _read_file_content(file_path)
    if isinstance(content, tuple):
        return content

    pattern = _get_command_pattern(command)
    match = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
    if not match:
        return (None, f"Command '{command}' not found in file.")

    first_match = _build_command_match(content, match)
    return (first_match.header, first_match.output)


def grep_output(output, grep_pattern, regex_flags=re.IGNORECASE):
    """Filter output to only show lines matching the grep pattern."""
    matching_lines = []
    for line in output.split("\n"):
        if re.search(grep_pattern, line, regex_flags):
            matching_lines.append(line)

    if matching_lines:
        return "\n".join(matching_lines)
    return ""


def grep_output_with_blocks(output, grep_pattern, regex_flags=re.IGNORECASE):
    """Return full config blocks containing any line matching the pattern."""
    lines = output.split("\n")

    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index]

        if not line.strip() or line.strip() == "!":
            index += 1
            continue

        if line and not line[0].isspace():
            block_start = index
            index += 1

            while index < len(lines):
                child_line = lines[index]
                if (
                    not child_line.strip()
                    or child_line.strip() == "!"
                    or (child_line and not child_line[0].isspace())
                ):
                    break
                index += 1

            blocks.append((block_start, index))
        else:
            index += 1

    result_blocks = []
    for block_start, block_end in blocks:
        block_lines = lines[block_start:block_end]
        if any(re.search(grep_pattern, line, regex_flags) for line in block_lines):
            result_blocks.append("\n".join(block_lines))

    if result_blocks:
        return "\n\n".join(result_blocks)
    return ""


def expand_file_patterns(patterns):
    """Expand file patterns, preserving order and de-duplicating matches."""
    files = []
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            files.extend(matches)
        elif os.path.isfile(pattern):
            files.append(pattern)
        else:
            print(f"Warning: No files matched pattern '{pattern}'", file=sys.stderr)

    seen = set()
    unique_files = []
    for file_path in files:
        if file_path not in seen:
            seen.add(file_path)
            unique_files.append(file_path)

    return unique_files


def parse_query(query_string):
    """Split a query string on the first colon only."""
    if ":" in query_string:
        command, pattern = query_string.split(":", 1)
        return (command.strip(), pattern.strip() if pattern.strip() else None)
    return (query_string.strip(), None)


def parse_q_query(query_string):
    """Parse a -q query string with optional + modifier."""
    query_string = query_string.strip()
    if not query_string:
        raise ValueError("Empty query is not allowed.")

    modifiers = frozenset()
    command_part = query_string
    grep_pattern = None

    if query_string[0].isalpha():
        command_part, grep_pattern = parse_query(query_string)
    else:
        if ":" not in query_string:
            raise ValueError(f"Invalid modifier syntax in query '{query_string}'.")

        modifier_text, remainder = query_string.split(":", 1)
        if not modifier_text:
            raise ValueError(f"Invalid modifier syntax in query '{query_string}'.")

        unknown_modifiers = [char for char in modifier_text if char not in DEFINED_Q_MODIFIERS]
        if unknown_modifiers:
            raise ValueError(
                f"Unknown modifier(s) '{''.join(unknown_modifiers)}' in query '{query_string}'."
            )

        modifiers = frozenset(modifier_text)
        command_part, grep_pattern = parse_query(remainder)

    if not command_part:
        raise ValueError(f"Missing command in query '{query_string}'.")

    return QuerySpec(command=command_part, grep_pattern=grep_pattern, modifiers=modifiers)


def parse_query_block(query_string):
    """Parse a -Q query string with optional + modifier."""
    query_string = query_string.strip()
    if not query_string:
        raise ValueError("Empty query is not allowed.")

    modifiers = frozenset()

    if query_string[0].isalpha():
        command, grep_pattern = parse_query(query_string)
    else:
        if ":" not in query_string:
            raise ValueError(f"Invalid modifier syntax in query '{query_string}'.")

        modifier_text, remainder = query_string.split(":", 1)
        if not modifier_text:
            raise ValueError(f"Invalid modifier syntax in query '{query_string}'.")

        unknown_modifiers = [
            char for char in modifier_text if char not in DEFINED_Q_BLOCK_MODIFIERS
        ]
        if unknown_modifiers:
            raise ValueError(
                f"Unknown modifier(s) '{''.join(unknown_modifiers)}' in query '{query_string}'."
            )

        modifiers = frozenset(modifier_text)
        command, grep_pattern = parse_query(remainder)

    if not command:
        raise ValueError(f"Missing command in query '{query_string}'.")

    return QuerySpec(command=command, grep_pattern=grep_pattern, use_blocks=True, modifiers=modifiers)


def render_match_output(command_match, query_spec):
    """Render the final extracted text that will be passed to TextFSM."""
    command_output = command_match.output

    if query_spec.grep_pattern:
        if query_spec.use_blocks:
            return grep_output_with_blocks(command_output, query_spec.grep_pattern)
        return grep_output(command_output, query_spec.grep_pattern)

    return command_output


def extract_rendered_matches(file_path, query_spec):
    """Extract and render one or more matches for a file/query pair."""
    if "+" in query_spec.modifiers:
        command_matches = extract_commands(file_path, query_spec.command)
        if isinstance(command_matches, tuple):
            return (None, command_matches[1])
    else:
        command_header, command_output = extract_command(file_path, query_spec.command)
        if command_header is None:
            return (None, command_output)
        command_matches = [CommandMatch(header=command_header, output=command_output)]

    rendered_matches = []
    for command_match in command_matches:
        rendered_matches.append(
            RenderedMatch(
                matched_command=_render_matched_command_header(command_match.header),
                rendered_output=render_match_output(command_match, query_spec),
            )
        )

    return (rendered_matches, None)


def parse_rendered_output_with_textfsm(template_text, rendered_output, textfsm_module):
    """Parse one rendered output string into TextFSM record dictionaries."""
    if not rendered_output.strip():
        return []

    parser = textfsm_module.TextFSM(io.StringIO(template_text))
    rows = parser.ParseText(rendered_output)
    return [dict(zip(parser.header, row)) for row in rows]


def classify_extract_status(error_message):
    """Map extraction errors into stable JSON statuses."""
    if error_message.startswith("Error reading file:"):
        return "extract_error"
    return "extract_not_found"


def summarize_file_status(match_entries):
    """Collapse per-match statuses into one file-level status."""
    statuses = [entry["status"] for entry in match_entries]

    if any(status == "parse_error" for status in statuses):
        if len(statuses) == 1 or all(status == "parse_error" for status in statuses):
            return "parse_error"
        return "partial_parse_error"

    if any(status == "ok" for status in statuses):
        return "ok"

    return "parse_empty"


def summarize_file_error(file_status, extract_error, match_entries):
    """Return the file-level error summary string, if any."""
    if file_status in {"extract_error", "extract_not_found"}:
        return extract_error

    parse_errors = [entry["error"] for entry in match_entries if entry["status"] == "parse_error"]
    if file_status == "parse_error":
        return parse_errors[0] if parse_errors else "Failed to parse extracted output."
    if file_status == "partial_parse_error":
        if len(parse_errors) == 1:
            return parse_errors[0]
        return f"{len(parse_errors)} extracted match(es) failed to parse."
    return None


def process_file(
    file_path,
    query_spec,
    query_mode,
    query_string,
    template_path,
    template_text,
    textfsm_module,
    parse_func=parse_rendered_output_with_textfsm,
):
    """Build one JSON result object for a file."""
    abs_file_path = os.path.abspath(file_path)
    basename = os.path.basename(file_path)

    rendered_matches, extract_error = extract_rendered_matches(file_path, query_spec)
    if rendered_matches is None:
        file_status = classify_extract_status(extract_error)
        return {
            "file": basename,
            "path": abs_file_path,
            "query_mode": query_mode,
            "query": query_string,
            "template": template_path,
            "status": file_status,
            "match_count": 0,
            "matches": [],
            "error": extract_error,
        }

    match_entries = []
    for rendered_match in rendered_matches:
        if not rendered_match.rendered_output.strip():
            match_entries.append(
                {
                    "matched_command": rendered_match.matched_command,
                    "status": "parse_empty",
                    "record_count": 0,
                    "records": [],
                    "error": None,
                }
            )
            continue

        try:
            records = parse_func(template_text, rendered_match.rendered_output, textfsm_module)
        except Exception as error:  # pragma: no cover - exercised via tests
            match_entries.append(
                {
                    "matched_command": rendered_match.matched_command,
                    "status": "parse_error",
                    "record_count": 0,
                    "records": [],
                    "error": str(error),
                }
            )
            continue

        status = "ok" if records else "parse_empty"
        match_entries.append(
            {
                "matched_command": rendered_match.matched_command,
                "status": status,
                "record_count": len(records),
                "records": records,
                "error": None,
            }
        )

    file_status = summarize_file_status(match_entries)
    return {
        "file": basename,
        "path": abs_file_path,
        "query_mode": query_mode,
        "query": query_string,
        "template": template_path,
        "status": file_status,
        "match_count": len(rendered_matches),
        "matches": match_entries,
        "error": summarize_file_error(file_status, None, match_entries),
    }


def validate_template_path(template_path):
    """Validate template path before attempting any batch processing."""
    abs_template_path = os.path.abspath(template_path)

    if os.path.isdir(abs_template_path):
        return (None, f"Template path '{abs_template_path}' is a directory.")
    if not os.path.exists(abs_template_path):
        return (None, f"Template path '{abs_template_path}' does not exist.")
    if not os.path.isfile(abs_template_path):
        return (None, f"Template path '{abs_template_path}' is not a file.")

    return (abs_template_path, None)


def load_template_text(template_path):
    """Load the template text from disk."""
    try:
        with open(template_path, "r") as file_handle:
            return (file_handle.read(), None)
    except OSError as error:
        return (None, f"Error reading template file '{template_path}': {error}")


def load_textfsm_module():
    """Import textfsm lazily so -h keeps working without the dependency."""
    try:
        import textfsm  # type: ignore
    except ModuleNotFoundError:
        print(
            "Error: TextFSM dependency not installed. "
            "Install it with: python3 -m pip install textfsm",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return textfsm


def build_argument_parser():
    """Create the CLI parser for showparse_fsm."""
    parser = argparse.ArgumentParser(
        description="Extract prompt-delimited command output and parse it with TextFSM.",
        epilog='''Supported modifiers:
  +    all matching commands for this query (-q, -Q)

Examples:
  showparse_fsm.py -q "show version" --template show_version.textfsm *.dat
  showparse_fsm.py -Q "show run:shutdown" --template interfaces.textfsm *.dat
  showparse_fsm.py -q "+:show run:hostname" --template hostnames.textfsm *.dat''',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "-q",
        "--query",
        action="append",
        help='Exactly one standard query in format "[+]command[:pattern]"',
    )
    parser.add_argument(
        "-Q",
        "--query-block",
        action="append",
        metavar="QUERY",
        help='Exactly one block query in format "[+]command[:pattern]"',
    )
    parser.add_argument(
        "--template",
        metavar="PATH",
        required=True,
        help="TextFSM template path to apply to each extracted result.",
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="File(s) to parse. Supports glob patterns like *.dat",
    )
    return parser


def main():
    parser = build_argument_parser()
    args = parser.parse_args()

    query_count = len(args.query or []) + len(args.query_block or [])
    if query_count != 1:
        parser.error("Exactly one -q/--query or -Q/--query-block is required.")

    if args.query and len(args.query) != 1:
        parser.error("Exactly one -q/--query is allowed.")
    if args.query_block and len(args.query_block) != 1:
        parser.error("Exactly one -Q/--query-block is allowed.")

    query_mode = "q" if args.query else "Q"
    query_string = args.query[0] if args.query else args.query_block[0]

    try:
        query_spec = parse_q_query(query_string) if args.query else parse_query_block(query_string)
    except ValueError as error:
        parser.error(str(error))

    files = expand_file_patterns(args.files)
    if not files:
        print("Error: No files found matching the specified pattern(s).", file=sys.stderr)
        sys.exit(1)

    template_path, template_path_error = validate_template_path(args.template)
    if template_path_error:
        print(f"Error: {template_path_error}", file=sys.stderr)
        sys.exit(1)

    template_text, template_error = load_template_text(template_path)
    if template_error:
        print(f"Error: {template_error}", file=sys.stderr)
        sys.exit(1)

    textfsm_module = load_textfsm_module()

    results = []
    for file_path in sorted(files):
        results.append(
            process_file(
                file_path=file_path,
                query_spec=query_spec,
                query_mode=query_mode,
                query_string=query_string,
                template_path=template_path,
                template_text=template_text,
                textfsm_module=textfsm_module,
            )
        )

    json.dump(results, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
