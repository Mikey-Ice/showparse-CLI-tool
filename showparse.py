#!/usr/bin/env python3
"""
showparse - Extract command output from network device collection files

Parses .dat files where commands are delimited by device prompts:
    Router1#show version
    Router1# show version
    <output>
    Router1#show ip route
    <output>

The prompt is detected as any non-whitespace string immediately before the
command at the start of a line, with zero or one space between prompt and
command. This is vendor-neutral (works with #, >, $, etc.) and avoids matching
indented commands (e.g., in "show history" output).

Usage:
    # Standard query mode
    showparse -q "show version" *.dat
    showparse -q "show run:interface" *.dat
    showparse -Q "show run:shutdown" *.dat
    showparse -q "show version:Cisco IOS" -Q "show run:shutdown" *.dat
"""
import argparse
import glob
import sys
import os
import re
import shlex
from dataclasses import dataclass


DEFINED_Q_MODIFIERS = {'%', '+', '/', '#'}
DEFINED_Q_BLOCK_MODIFIERS = {'%', '+', '@', '/', '#'}


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


def print_banner(filename):
    """Print a banner with the filename in yellow"""
    basename = os.path.basename(filename)
    # Create banner: --------------------<<<filename>>>--------------------
    banner_text = f"<<< {basename} >>>"
    # Total width of about 70 characters
    padding = (70 - len(banner_text)) // 2
    dashes = "-" * padding
    banner_line = f"{dashes}{banner_text}{dashes}"
    # ANSI yellow text: \033[33m ... \033[0m (reset)
    print(f"\n\033[33m{banner_line}\033[0m\n")


def extract_commands(file_path, command):
    """
    Extract all outputs for commands matching a prefix from a .dat file.
    
    The file format uses device prompts to delimit commands:
        Router1#show version
        Router1# show version
        <output>
        Router1#show ip route
        <output>
    
    The prompt is any non-whitespace string immediately preceding the command,
    or separated from it by exactly one space, at the start of a line. Output
    continues until that same prompt appears again at the start of a line.
    
    This avoids matching indented commands (e.g., in "show history" output).
    
    Returns a list of CommandMatch objects, or (None, error_message) if not found.
    """
    try:
        with open(file_path, 'r') as f:
            content = f.read()
    except IOError as e:
        return (None, f"Error reading file: {e}")
    
    # Match prompt + command at start of line, allowing IOS-style "promptcmd"
    # and NX-OS-style "prompt cmd" but not multiple spaces or indentation.
    # Restrict prompt tokens to common prompt-ending characters so metadata
    # lines like "!Command:" are not treated as device prompts.
    pattern = rf"^(\S*[#>$%])( ?{re.escape(command)}[^\n]*)"
    matches = list(re.finditer(pattern, content, re.MULTILINE | re.IGNORECASE))

    if not matches:
        return (None, f"Command '{command}' not found in file.")

    command_matches = []
    for match in matches:
        prompt = match.group(1)  # e.g., "Router1#"
        command_line = match.group(2)  # e.g., "show running-config"
        command_header = prompt + command_line  # Full line: "Router1#show running-config"

        output_start = match.end()

        # Output ends at the next occurrence of the same prompt at start of line.
        end_pattern = rf"^{re.escape(prompt)}"
        end_match = re.search(end_pattern, content[output_start:], re.MULTILINE)

        if end_match:
            command_output = content[output_start:output_start + end_match.start()].strip()
        else:
            command_output = content[output_start:].strip()

        command_matches.append(CommandMatch(header=command_header, output=command_output))

    return command_matches


def extract_command(file_path, command):
    """
    Extract the first output for a command prefix from a .dat file.

    Returns tuple of (command_header, command_output) or (None, error_message) if not found.
    """
    result = extract_commands(file_path, command)

    if isinstance(result, tuple):
        return result

    first_match = result[0]
    return (first_match.header, first_match.output)


def get_regex_flags(modifiers):
    """Return regex flags for a query based on its modifiers."""
    if '/' in modifiers:
        return 0
    return re.IGNORECASE


def grep_output(output, grep_pattern, regex_flags=re.IGNORECASE):
    """
    Filter output to only show lines matching the grep pattern.
    Returns all matching lines (like grep).
    """
    matching_lines = []
    for line in output.split('\n'):
        if re.search(grep_pattern, line, regex_flags):
            matching_lines.append(line)
    
    if matching_lines:
        return '\n'.join(matching_lines)
    else:
        return ""


def grep_output_matches_only(output, grep_pattern, regex_flags=re.IGNORECASE):
    """
    Filter output to only show the matched substring from each matching line.
    Returns the first regex match per matching line.
    """
    matching_parts = []
    for line in output.split('\n'):
        match = re.search(grep_pattern, line, regex_flags)
        if match:
            matching_parts.append(match.group(0))

    if matching_parts:
        return '\n'.join(matching_parts)
    else:
        return ""


def _trim_match(line, grep_pattern, regex_flags=re.IGNORECASE):
    """Return the matched substring for a line, or the original line if it did not match."""
    match = re.search(grep_pattern, line, regex_flags)
    if match:
        return match.group(0)
    return line


def count_non_empty_lines(output):
    """Return the number of non-empty rendered lines, or empty string when no output remains."""
    if not output.strip():
        return ""
    return str(sum(1 for line in output.splitlines() if line.strip()))


def grep_output_with_blocks(output, grep_pattern, regex_flags=re.IGNORECASE):
    """
    Filter output to show the full config block containing any line matching the grep pattern.
    Works bi-directionally:
    - If match is a parent (not indented): show parent + children
    - If match is a child (indented): show parent + all siblings
    
    Example 1 (match on parent):
        -g "interface Ethernet0/0" shows:
        interface Ethernet0/0      <- matched
         ip address 192.168.1.1    <- child
         no shutdown               <- child
    
    Example 2 (match on child):
        -g "shutdown" shows:
        interface Ethernet0/1      <- parent (context)
         no ip address             <- sibling
         shutdown                  <- matched
    """
    lines = output.split('\n')
    
    # First pass: identify all config blocks (parent + children)
    # Each block is a tuple of (start_index, end_index)
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Skip empty lines and '!' separators
        if not line.strip() or line.strip() == '!':
            i += 1
            continue
        
        # If this is a non-indented line, it's a parent - start a new block
        if line and not line[0].isspace():
            block_start = i
            i += 1
            
            # Capture all following indented child lines
            while i < len(lines):
                child_line = lines[i]
                # Stop if we hit a non-indented line or '!' separator or empty line
                if not child_line.strip() or child_line.strip() == '!' or (child_line and not child_line[0].isspace()):
                    break
                i += 1
            
            block_end = i
            blocks.append((block_start, block_end))
        else:
            i += 1
    
    # Second pass: find blocks containing a match
    matched_blocks = set()
    for block_start, block_end in blocks:
        block_lines = lines[block_start:block_end]
        for line in block_lines:
            if re.search(grep_pattern, line, regex_flags):
                matched_blocks.add((block_start, block_end))
                break  # Found a match in this block, move to next block
    
    # Build result from matched blocks
    result_blocks = []
    for block_start, block_end in sorted(matched_blocks):
        block_text = '\n'.join(lines[block_start:block_end])
        result_blocks.append(block_text)
    
    if result_blocks:
        # Separate multiple blocks with a blank line for readability
        return '\n\n'.join(result_blocks)
    else:
        return ""


def grep_output_with_blocks_selective(
    output,
    grep_pattern,
    matched_children_only=False,
    trim_matches=False,
    regex_flags=re.IGNORECASE,
):
    """
    Filter output using config blocks with optional selective child output and line trimming.

    Behavior:
    - Parent match: return full block
    - Child match + matched_children_only=False: return full block
    - Child match + matched_children_only=True: return parent + matching child lines only
    - trim_matches=True trims matching lines down to the matched substring
    """
    lines = output.split('\n')

    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if not line.strip() or line.strip() == '!':
            i += 1
            continue

        if line and not line[0].isspace():
            block_start = i
            i += 1

            while i < len(lines):
                child_line = lines[i]
                if not child_line.strip() or child_line.strip() == '!' or (child_line and not child_line[0].isspace()):
                    break
                i += 1

            blocks.append((block_start, i))
        else:
            i += 1

    result_blocks = []
    for block_start, block_end in blocks:
        block_lines = lines[block_start:block_end]
        parent_line = block_lines[0]
        child_lines = block_lines[1:]

        parent_matches = bool(re.search(grep_pattern, parent_line, regex_flags))
        matching_children = [
            child_line for child_line in child_lines
            if re.search(grep_pattern, child_line, regex_flags)
        ]

        if not parent_matches and not matching_children:
            continue

        if parent_matches or not matched_children_only:
            selected_lines = block_lines
        else:
            selected_lines = [parent_line] + matching_children

        if trim_matches:
            trimmed_lines = []
            for line in selected_lines:
                trimmed_lines.append(_trim_match(line, grep_pattern, regex_flags))
            selected_lines = trimmed_lines

        result_blocks.append('\n'.join(selected_lines))

    if result_blocks:
        return '\n\n'.join(result_blocks)
    else:
        return ""


def expand_file_patterns(patterns):
    """
    Expand file patterns (globs) and return list of matching files.
    Handles both glob patterns and direct file paths.
    """
    files = []
    for pattern in patterns:
        # Try glob expansion
        matches = glob.glob(pattern)
        if matches:
            files.extend(matches)
        elif os.path.isfile(pattern):
            # Direct file path
            files.append(pattern)
        else:
            print(f"Warning: No files matched pattern '{pattern}'", file=sys.stderr)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_files = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique_files.append(f)
    
    return unique_files


def parse_query(query_string):
    """
    Parse a query string in format "command:grep_pattern"
    Split on first colon only, so patterns can contain colons.
    
    Returns tuple of (command, grep_pattern) or (command, None) if no pattern.
    """
    if ':' in query_string:
        command, pattern = query_string.split(':', 1)
        return (command.strip(), pattern.strip() if pattern.strip() else None)
    else:
        return (query_string.strip(), None)


def parse_q_query(query_string):
    """
    Parse a -q query string.

    Supported forms:
        command
        command:pattern
        modifiers:command:pattern
    """
    query_string = query_string.strip()
    if not query_string:
        raise ValueError("Empty query is not allowed.")

    modifiers = frozenset()
    command_part = query_string
    grep_pattern = None

    first_char = query_string[0]
    if first_char.isalpha():
        command_part, grep_pattern = parse_query(query_string)
    else:
        if ':' not in query_string:
            raise ValueError(f"Invalid modifier syntax in query '{query_string}'.")

        modifier_text, remainder = query_string.split(':', 1)
        if not modifier_text:
            raise ValueError(f"Invalid modifier syntax in query '{query_string}'.")

        if '@' in modifier_text:
            raise ValueError(f"Modifier '@' is only supported with -Q in query '{query_string}'.")

        unknown_modifiers = [char for char in modifier_text if char not in DEFINED_Q_MODIFIERS]
        if unknown_modifiers:
            raise ValueError(
                f"Unknown modifier(s) '{''.join(unknown_modifiers)}' in query '{query_string}'."
            )

        modifiers = frozenset(modifier_text)
        command_part, grep_pattern = parse_query(remainder)

    if not command_part:
        raise ValueError(f"Missing command in query '{query_string}'.")

    if ('%' in modifiers or '/' in modifiers) and not grep_pattern:
        raise ValueError(f"Modifier(s) require a grep pattern in query '{query_string}'.")

    return QuerySpec(command=command_part, grep_pattern=grep_pattern, modifiers=modifiers)


def parse_query_block(query_string):
    """Parse a -Q query string."""
    query_string = query_string.strip()
    if not query_string:
        raise ValueError("Empty query is not allowed.")

    modifiers = frozenset()
    first_char = query_string[0]

    if first_char.isalpha():
        command, grep_pattern = parse_query(query_string)
    else:
        if ':' not in query_string:
            raise ValueError(f"Invalid modifier syntax in query '{query_string}'.")

        modifier_text, remainder = query_string.split(':', 1)
        if not modifier_text:
            raise ValueError(f"Invalid modifier syntax in query '{query_string}'.")

        unknown_modifiers = [char for char in modifier_text if char not in DEFINED_Q_BLOCK_MODIFIERS]
        if unknown_modifiers:
            raise ValueError(
                f"Unknown modifier(s) '{''.join(unknown_modifiers)}' in query '{query_string}'."
            )

        modifiers = frozenset(modifier_text)
        command, grep_pattern = parse_query(remainder)

    if ('%' in modifiers or '@' in modifiers or '/' in modifiers) and not grep_pattern:
        raise ValueError(f"Modifier(s) require a grep pattern in query '{query_string}'.")

    return QuerySpec(command=command, grep_pattern=grep_pattern, use_blocks=True, modifiers=modifiers)


def collect_ordered_query_specs(argv):
    """Collect -q/-Q queries in CLI order and parse them into query specs."""
    query_specs = []
    args_iter = iter(range(len(argv)))
    for i in args_iter:
        arg = argv[i]
        if arg in ('-q', '--query'):
            if i + 1 < len(argv):
                query_specs.append(parse_q_query(argv[i + 1]))
                next(args_iter, None)
        elif arg.startswith('--query='):
            query_specs.append(parse_q_query(arg.split('=', 1)[1]))
        elif arg in ('-Q', '--query-block'):
            if i + 1 < len(argv):
                query_specs.append(parse_query_block(argv[i + 1]))
                next(args_iter, None)
        elif arg.startswith('--query-block='):
            query_specs.append(parse_query_block(arg.split('=', 1)[1]))
    return query_specs


def process_query(file_path, query_spec):
    """
    Process a single query against a file.
    Returns the command output string (no header - just the output).
    
    If grep_pattern is specified, returns only matching content.
    If no grep_pattern, returns full command output.
    """
    if '+' in query_spec.modifiers:
        command_matches = extract_commands(file_path, query_spec.command)
        if isinstance(command_matches, tuple):
            return command_matches[1]
    else:
        command_header, command_output = extract_command(file_path, query_spec.command)

        if command_header is None:
            # Error case - command_output contains error message
            return command_output

        command_matches = [CommandMatch(header=command_header, output=command_output)]

    processed_outputs = []
    regex_flags = get_regex_flags(query_spec.modifiers)
    for command_match in command_matches:
        command_output = command_match.output
        if query_spec.grep_pattern:
            if query_spec.use_blocks:
                if '@' in query_spec.modifiers or '%' in query_spec.modifiers:
                    filtered_output = grep_output_with_blocks_selective(
                        command_output,
                        query_spec.grep_pattern,
                        matched_children_only='@' in query_spec.modifiers,
                        trim_matches='%' in query_spec.modifiers,
                        regex_flags=regex_flags,
                    )
                else:
                    filtered_output = grep_output_with_blocks(
                        command_output,
                        query_spec.grep_pattern,
                        regex_flags=regex_flags,
                    )
            elif '%' in query_spec.modifiers:
                filtered_output = grep_output_matches_only(
                    command_output,
                    query_spec.grep_pattern,
                    regex_flags=regex_flags,
                )
            else:
                filtered_output = grep_output(
                    command_output,
                    query_spec.grep_pattern,
                    regex_flags=regex_flags,
                )
            if filtered_output:
                processed_outputs.append(filtered_output)
        else:
            processed_outputs.append(command_output)

    rendered_output = '\n\n'.join(processed_outputs)

    if '#' in query_spec.modifiers:
        return count_non_empty_lines(rendered_output)

    return rendered_output


def get_query_results(file_path, all_queries):
    """Run all queries for a file and return outputs in the same order."""
    query_results = []
    for query_spec in all_queries:
        query_results.append(process_query(file_path, query_spec))
    return query_results


def get_file_output(file_path, args, all_queries):
    """
    Get the complete output for a file.
    Returns the output string that would be displayed for this file.
    Used by notes mode to group files by unique output.
    """
    query_results = get_query_results(file_path, all_queries)

    # AND mode: check if ALL queries returned content
    if args.and_mode and any(not result.strip() for result in query_results):
        return ""  # Empty output for AND mode failures

    # Combine results with blank line separator
    return '\n\n'.join(query_results)


def print_unique_banner(index, total, match_count):
    """Print a banner for unique output in notes mode"""
    match_text = f"matched {match_count} time" if match_count == 1 else f"matched {match_count} times"
    banner_text = f"<<< Unique Output {index} of {total} | {match_text} >>>"
    padding = (70 - len(banner_text)) // 2
    dashes = "-" * padding
    print(f"\n{dashes}{banner_text}{dashes}\n")


def build_notes_report(notes_collected, command_summary):
    """Build the final notes report text."""
    report_lines = [
        "=" * 70,
        "NOTES REPORT",
        "=" * 70,
        f"Command: {command_summary}",
        "",
    ]
    report_lines.extend(f"{filename}:{note}" for filename, note in sorted(notes_collected))
    return "\n" + "\n".join(report_lines) + "\n"


def build_notes_command_summary(argv):
    """Build a compact, reproducible showparse command summary for notes reports."""
    retained_tokens = ["showparse"]
    i = 0
    while i < len(argv):
        arg = argv[i]

        if arg in ("-q", "--query", "-Q", "--query-block"):
            retained_tokens.append(arg)
            if i + 1 < len(argv):
                retained_tokens.append(argv[i + 1])
                i += 2
                continue
        elif arg.startswith("--query=") or arg.startswith("--query-block="):
            retained_tokens.append(arg)
        elif arg in ("-A", "--and", "-r", "--raw"):
            retained_tokens.append(arg)
        elif arg in ("-n", "--notes"):
            pass
        elif arg in ("-o", "--output-file"):
            if i + 1 < len(argv):
                i += 2
                continue
        elif arg.startswith("--output-file="):
            pass

        i += 1

    return shlex.join(retained_tokens)


def validate_output_file_path(output_file_path):
    """Validate a notes report output path without creating or truncating it."""
    if os.path.isdir(output_file_path):
        return f"Output file path '{output_file_path}' is a directory."

    parent_dir = os.path.dirname(output_file_path) or "."
    if not os.path.isdir(parent_dir):
        return (
            f"Output file path '{output_file_path}' is invalid: "
            f"parent directory '{parent_dir}' does not exist."
        )

    return None


def print_notes_progress(completed, total):
    """Render notes-mode phase-1 progress to stderr on a single live line."""
    print(f"\r({completed}/{total})", end="", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(
        description='Extract command output from network device collection files (prompt-based parsing)',
        epilog='''Modifiers:
  %    matched text only (-q, -Q)
  #    count non-empty rendered lines (-q, -Q)
  /    case-sensitive pattern matching (-q, -Q)
  +    all matching commands for this query (-q, -Q)
  @    parent + matched child lines only (-Q only)

Examples:
  showparse -q "show version" *.dat
  showparse -q "#:show version" *.dat
  showparse -Q "@%:show run:switchport mode access|shutdown" *.dat''',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        '-q', '--query',
        action='append',
        help='Query in format "[modifiers:]command[:pattern]" (can be repeated)'
    )
    parser.add_argument(
        '-Q', '--query-block',
        action='append',
        metavar='QUERY',
        help='Block query in format "[modifiers:]command:pattern" (can be repeated)'
    )
    
    # Query logic mode
    parser.add_argument(
        '-A', '--and',
        action='store_true',
        dest='and_mode',
        help='AND mode: ALL queries must match for a file to show output. Use with -q/-Q.'
    )
    
    # Output mode
    parser.add_argument(
        '-r', '--raw',
        action='store_true',
        help='Raw output mode: no banners, prefix each line with filename (grep-style)'
    )
    parser.add_argument(
        '-n', '--notes',
        action='store_true',
        help='Notes mode: interactive annotation. Enter to skip, q to quit. Prints report at end.'
    )
    parser.add_argument(
        '-o', '--output-file',
        metavar='PATH',
        help='Save the final notes report to a file. Only valid with -n.'
    )
    
    # Files
    parser.add_argument(
        'files',
        nargs='+',
        help='File(s) to parse. Supports glob patterns like *.dat'
    )
    
    args = parser.parse_args()
    
    # Validate: can't use both -r and -n
    if args.raw and args.notes:
        parser.error("Cannot use both -r and -n. Raw mode and notes mode are mutually exclusive.")
    if args.output_file and not args.notes:
        parser.error("-o/--output-file can only be used with -n/--notes.")
    
    if not args.query and not args.query_block:
        parser.error("At least one -q/--query or -Q/--query-block is required")

    try:
        all_queries = collect_ordered_query_specs(sys.argv[1:])
    except ValueError as e:
        parser.error(str(e))

    # Expand file patterns
    files = expand_file_patterns(args.files)
    
    if not files:
        print("Error: No files found matching the specified pattern(s).", file=sys.stderr)
        sys.exit(1)

    if args.notes and args.output_file:
        path_error = validate_output_file_path(args.output_file)
        if path_error:
            print(f"Error: {path_error}", file=sys.stderr)
            sys.exit(1)
    
    # Notes mode: special handling with deduplication
    if args.notes:
        # Phase 1: Collect all outputs and group by unique content
        output_groups = {}  # {output_content: [filenames]}
        total_files = len(files)
        for completed, file_path in enumerate(sorted(files), 1):
            filename = os.path.basename(file_path)
            output = get_file_output(file_path, args, all_queries)
            output_groups.setdefault(output, []).append(filename)
            print_notes_progress(completed, total_files)

        print(file=sys.stderr)
        
        # Phase 2: Present each unique output and collect notes
        notes_collected = []
        notes_interrupted = False
        unique_outputs = list(output_groups.keys())
        
        for i, output in enumerate(unique_outputs, 1):
            filenames = output_groups[output]
            
            # Print unique output banner
            print_unique_banner(i, len(unique_outputs), len(filenames))
            
            # Show output (or "(empty)" if blank)
            if output.strip():
                print(output)
            else:
                print("(empty)")
            
            # Prompt for note
            try:
                note = input("Notes: ")
                if note.lower() == 'q':
                    break  # Quit early
                if note.strip():
                    # Apply note to ALL files with this output
                    for fn in filenames:
                        notes_collected.append((fn, note))
            except KeyboardInterrupt:
                notes_interrupted = True
                print()
                break
            except EOFError:
                break
        
        # Phase 3: Print notes report
        if notes_interrupted:
            return
        if notes_collected:
            command_summary = build_notes_command_summary(sys.argv[1:])
            report_text = build_notes_report(notes_collected, command_summary)
            if args.output_file:
                try:
                    with open(args.output_file, 'w') as output_file:
                        output_file.write(report_text)
                except OSError as e:
                    print(f"Error writing notes report to '{args.output_file}': {e}", file=sys.stderr)
                    sys.exit(1)
                print(f"\nNotes report saved to {args.output_file}\n")
            else:
                print(report_text)
        else:
            print("\nNo notes collected.\n")
    
    else:
        # Normal mode (not notes): process and display each file
        try:
            for file_path in sorted(files):
                filename = os.path.basename(file_path)

                query_results = get_query_results(file_path, all_queries)

                # AND mode: skip files where any query returned empty
                if args.and_mode and any(not result.strip() for result in query_results):
                    continue

                if not args.raw:
                    print_banner(file_path)

                for i, output in enumerate(query_results):
                    if args.raw:
                        # Raw mode: prefix each line with filename (no separators)
                        for line in output.split('\n'):
                            if line.strip():  # Skip empty lines
                                print(f"{filename}:{line}")
                    else:
                        print(output)
                        # Add visual separator between queries (but not after the last one)
                        if i < len(query_results) - 1:
                            print()
                            print("-" * 40)
                            print()
            
            # Add a newline at the end for cleaner output (not in raw mode)
            if not args.raw:
                print()
        except KeyboardInterrupt:
            print()
            sys.exit(130)


if __name__ == '__main__':
    main()
