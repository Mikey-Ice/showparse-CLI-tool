#!/usr/bin/env python3
"""
showparse - Extract command output from network device collection files

Parses captures where commands are delimited by device prompts:
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
import io
import sys
import os
import re
import shlex
import stat
from dataclasses import dataclass
from enum import Enum


DEFINED_Q_MODIFIERS = {'%', '+', '/', '#', '~'}
DEFINED_Q_BLOCK_MODIFIERS = {'%', '+', '@', '/', '#', '~'}
CAPTURE_CHUNK_SIZE = 64 * 1024
PROMPT_LINE_PATTERN = re.compile(r"^\S*[#>$%][^\n]*", re.MULTILINE)
NOTES_HELP = """Notes controls:
  Text     Apply a note to every file in the displayed group (q/Q are text).
  Enter    Skip this group when the line is empty.
  /done    Finish and save completed notes.
  /help    Show these controls and stay on this group.
  Ctrl-C   Cancel annotation and discard notes without writing a report.
  //text   Record /text (double the first slash for a literal leading slash).

Slash commands occupy a whole line and are case-insensitive; surrounding spaces
are ignored. Unknown slash commands are rejected without advancing the group.
Reaching the last group or end of input also finishes using completed notes.
With -o, finishing saves quietly; otherwise the report is printed.
No notes means no report. Existing-report confirmation and save errors still appear."""


@dataclass(frozen=True)
class QuerySpec:
    """Parsed query definition for one -q or -Q flag."""
    command: str
    grep_pattern: str | None
    use_blocks: bool = False
    modifiers: frozenset[str] = frozenset()


class QueryStatus(Enum):
    MATCH = 'match'
    NO_MATCH = 'no_match'
    READ_ERROR = 'read_error'


@dataclass(frozen=True)
class QueryResult:
    """Keep selected capture text separate from query status and diagnostics."""
    status: QueryStatus
    output: str = ''
    error: str = ''

    @classmethod
    def from_output(cls, output):
        status = QueryStatus.MATCH if output.strip() else QueryStatus.NO_MATCH
        return cls(status, output=output)


@dataclass(frozen=True)
class CommandMatch:
    """One prompt-delimited command match extracted from a file."""
    header: str
    output: str


class _ActiveCommand:
    """Feed a command's stripped lines to shared selections as text arrives.

    Keep the last nonblank line pending: only at the next nonblank line do we
    know its trailing spaces belong to the output rather than the final strip.
    Runs of intervening blank lines are compressed until that decision is known.
    """
    def __init__(self, order, header, queries):
        self.order = order
        self.header = header
        self.queries = queries
        self.query_indices = []
        self.selections = None
        self.line_selections = []
        self.partial = ''
        self.pending = None
        self.blank_runs = []

    def _prepare(self):
        if self.selections is not None:
            return
        groups = {}
        for index in self.query_indices:
            query = self.queries[index]
            key = ((query.grep_pattern, query.use_blocks, query.modifiers - {'+', '#', '~'})
                   if query.grep_pattern else (None,))
            groups.setdefault(key, []).append(index)
        self.selections = []
        for indices in groups.values():
            keep_text = any('#' not in self.queries[index].modifiers for index in indices)
            count_lines = any('#' in self.queries[index].modifiers for index in indices)
            selection = _CommandSelection(self.queries[indices[0]], keep_text, count_lines)
            self.selections.append((indices, selection))
            if selection.search:
                self.line_selections.append(selection)

    def _emit(self, line, repeats=1):
        for selection in self.line_selections:
            selection.add_line(line, repeats)

    def _accept_line(self, line):
        if self.pending is None:
            line = line.lstrip()
            if line:
                self.pending = line
        elif line.strip():
            self._emit(self.pending)
            for blank, repeats in self.blank_runs:
                self._emit(blank, repeats)
            self.blank_runs.clear()
            self.pending = line
        elif self.blank_runs and self.blank_runs[-1][0] == line:
            blank, repeats = self.blank_runs[-1]
            self.blank_runs[-1] = (blank, repeats + 1)
        else:
            self.blank_runs.append((line, 1))

    def feed(self, text):
        self._prepare()
        for _, selection in self.selections:
            if not selection.search:
                selection.selected.add_unfiltered(text)
        if not self.line_selections:
            return
        lines = (self.partial + text).split('\n')
        self.partial = lines.pop()
        for line in lines:
            self._accept_line(line)

    def results(self):
        self._prepare()
        self._accept_line(self.partial)
        if self.pending is not None:
            self._emit(self.pending.rstrip())
        for indices, selection in self.selections:
            selected = selection.finish()
            if selected is None:
                continue
            for index in indices:
                query = self.queries[index]
                output = selected.text() if '#' not in query.modifiers else ''
                count = selected.line_count
                if '~' in query.modifiers:
                    header = _render_matched_command_header(self.header)
                    count += _count_lines(header)
                    if '#' not in query.modifiers:
                        output = header + '\n' + output if selected.has_text else header
                yield self.order, index, output, count


def _iter_selected_commands(stream, queries):
    """Read forward once, yielding rendered selections and counts by query.

    Scan bounded chunks at line boundaries so regex searches run over batches of
    lines. Open commands retain selection state. Prompts and command prefixes
    keep their existing matching rules, including overlapping prompt tokens.
    """
    searches = {}
    for index, query in enumerate(queries):
        searches.setdefault(query.command, []).append(index)
    patterns = {command: re.compile(_get_command_pattern(command), re.IGNORECASE | re.MULTILINE)
                for command in searches}
    active = {}
    order = 0

    while chunk := stream.read(CAPTURE_CHUNK_SIZE):
        if not chunk.endswith('\n'):
            chunk += stream.readline()  # Keep command headers whole across chunks.
        cursor = 0
        for candidate in PROMPT_LINE_PATTERN.finditer(chunk):
            start, end = candidate.span()
            piece = chunk[cursor:start]
            for block in active.values():
                block.feed(piece)

            for prompt in list(active):
                if chunk.startswith(prompt, start):
                    yield from active.pop(prompt).results()

            if not searches and not active:
                return  # All first-match queries have their complete command output.

            # A different prompt inside an open block remains part of its output.
            header = candidate.group(0)
            for block in active.values():
                block.feed(header)

            for command, indices in list(searches.items()):
                match = patterns[command].match(chunk, start)
                if match is None:
                    continue
                prompt = match.group(1)
                if prompt not in active:
                    active[prompt] = _ActiveCommand(order, header, queries)
                active[prompt].query_indices.extend(indices)
                remaining = [index for index in indices if '+' in queries[index].modifiers]
                if remaining:
                    searches[command] = remaining
                else:
                    del searches[command]
            order += 1
            cursor = end

        piece = chunk[cursor:]
        for block in active.values():
            block.feed(piece)

    for block in active.values():
        yield from block.results()


def _read_file_content(file_path):
    """Buffered helper compatibility: return text or the legacy error tuple."""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except IOError as e:
        return (None, f"Error reading file: {e}")


def _get_command_pattern(command):
    """Return the prompt-delimited regex used for command extraction."""
    return rf"^(\S*[#>$%])( ?{re.escape(command)}[^\n]*)"


def _build_command_match(content, match):
    """Build a CommandMatch from one prompt-delimited regex match."""
    prompt = match.group(1)  # e.g., "Router1#"
    command_line = match.group(2)  # e.g., "show running-config"
    command_header = prompt + command_line  # Full line: "Router1#show running-config"

    output_start = match.end()

    # Output ends at the next occurrence of the same prompt at start of line.
    end_pattern = re.compile(rf"^{re.escape(prompt)}", re.MULTILINE)
    end_match = end_pattern.search(content, output_start)

    if end_match:
        command_output = content[output_start:end_match.start()].strip()
    else:
        command_output = content[output_start:].strip()

    return CommandMatch(header=command_header, output=command_output)


def _render_matched_command_header(command_header):
    """Return the matched command line without the device prompt token."""
    return re.sub(r"^\S*[#>$%] ?", "", command_header, count=1)


def extract_commands(file_path, command, *, content=None):
    """
    Extract all outputs for commands matching a prefix from a capture.
    
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
    if content is None:
        content = _read_file_content(file_path)
    if isinstance(content, tuple):
        return content
    
    # Match prompt + command at start of line, allowing IOS-style "promptcmd"
    # and NX-OS-style "prompt cmd" but not multiple spaces or indentation.
    # Restrict prompt tokens to common prompt-ending characters so metadata
    # lines like "!Command:" are not treated as device prompts.
    pattern = _get_command_pattern(command)
    matches = list(re.finditer(pattern, content, re.MULTILINE | re.IGNORECASE))

    if not matches:
        return (None, f"Command '{command}' not found in file.")

    return [_build_command_match(content, match) for match in matches]


def extract_command(file_path, command, *, content=None):
    """
    Extract the first output for a command prefix from a capture.

    Returns tuple of (command_header, command_output) or (None, error_message) if not found.
    """
    if content is None:
        content = _read_file_content(file_path)
    if isinstance(content, tuple):
        return content

    pattern = _get_command_pattern(command)
    match = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
    if not match:
        return (None, f"Command '{command}' not found in file.")

    first_match = _build_command_match(content, match)
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
    search = re.compile(grep_pattern, regex_flags).search
    matching_lines = []
    for line in output.split('\n'):
        if search(line):
            matching_lines.append(line)
    return '\n'.join(matching_lines)


def grep_output_matches_only(output, grep_pattern, regex_flags=re.IGNORECASE):
    """
    Filter output to only show the matched substring from each matching line.
    Returns the first regex match per matching line.
    """
    search = re.compile(grep_pattern, regex_flags).search
    matching_parts = []
    for line in output.split('\n'):
        match = search(line)
        if match:
            matching_parts.append(match.group(0))

    return '\n'.join(matching_parts)


def _trim_match(line, search):
    """Return the matched substring for a line, or the original line if it did not match."""
    match = search(line)
    if match:
        return match.group(0)
    return line


def _count_lines(output):
    """Count rendered lines, including Python's Unicode line separators."""
    return sum(1 for line in output.splitlines() if line.strip())


def count_non_empty_lines(output):
    """Return the number of non-empty rendered lines, or empty string when no output remains."""
    if not output.strip():
        return ""
    return str(_count_lines(output))


class _ConfigBlocks:
    """Recognize parent/child boundaries for both buffered and streamed filters."""
    def __init__(self):
        self.lines = []

    def push(self, line):
        separator = not line.strip() or line.strip() == '!'
        parent = line and not line[0].isspace()
        completed = self.finish() if separator or parent else None
        if parent and not separator:
            self.lines = [line]
        elif not separator and self.lines:
            self.lines.append(line)
        return completed

    def finish(self):
        lines, self.lines = self.lines, []
        return lines


def _iter_config_blocks(lines):
    """Yield each unindented parent with its children, in encounter order."""
    blocks = _ConfigBlocks()
    for line in lines:
        block = blocks.push(line)
        if block:
            yield block
    block = blocks.finish()
    if block:
        yield block


def _select_config_block(block, search, matched_children_only, trim_matches):
    """Return selected lines from one block, or None when nothing matched."""
    if search(block[0]):
        selected = block
    elif matched_children_only:
        children = [line for line in block[1:] if search(line)]
        if not children:
            return None
        selected = [block[0]] + children
    elif any(search(line) for line in block[1:]):
        selected = block
    else:
        return None
    if trim_matches:
        selected = [_trim_match(line, search) for line in selected]
    return selected


def grep_output_with_blocks(output, grep_pattern, regex_flags=re.IGNORECASE):
    """Select full config blocks containing a matching parent or child line."""
    return grep_output_with_blocks_selective(output, grep_pattern, regex_flags=regex_flags)


def grep_output_with_blocks_selective(
    output,
    grep_pattern,
    matched_children_only=False,
    trim_matches=False,
    regex_flags=re.IGNORECASE,
):
    """Select full blocks or parents with matching children; optionally trim matches.

    A matching parent always selects its entire block. Blank lines and standalone
    exclamation marks separate blocks; orphan indented lines have no parent.
    """
    search = re.compile(grep_pattern, regex_flags).search
    result_blocks = []
    for block in _iter_config_blocks(output.split('\n')):
        selected = _select_config_block(block, search, matched_children_only, trim_matches)
        if selected is not None:
            result_blocks.append('\n'.join(selected))
    return '\n\n'.join(result_blocks)


class _SelectedText:
    """Accumulate selected text or just its count, preserving empty separators."""
    def __init__(self, keep_text, count_lines, separator):
        self.stream = io.StringIO() if keep_text else None
        self.count_lines = count_lines
        self.separator = separator
        self.pieces = 0
        self.has_text = False
        self.line_count = 0
        self.trailing_parts = []

    def add_unfiltered(self, text):
        """Handle whole chunks when no regex needs individual lines.

        Reader pieces meet at newlines (or immediately before the newline after
        a nested prompt header), so nonempty line counts are additive. Deferred
        trailing whitespace preserves the original command-wide strip operation.
        """
        if self.count_lines:
            self.line_count += _count_lines(text)
        if not self.has_text:
            text = text.lstrip()
        kept = text.rstrip()
        if kept:
            if self.stream is not None:
                self.stream.writelines(self.trailing_parts)
                self.stream.write(kept)
            self.trailing_parts.clear()
            self.has_text = True
        if self.stream is not None and text[len(kept):]:
            self.trailing_parts.append(text[len(kept):])

    def add(self, text, repeats=1):
        self.has_text |= bool(text) or self.pieces + repeats > 1
        if self.count_lines:
            self.line_count += _count_lines(text) * repeats
        if self.stream is not None:
            for _ in range(repeats):
                if self.pieces:
                    self.stream.write(self.separator)
                self.stream.write(text)
                self.pieces += 1
        else:
            self.pieces += repeats

    def text(self):
        return self.stream.getvalue() if self.stream is not None else ''


class _CommandSelection:
    """Filter one command incrementally; buffer at most one config block."""
    def __init__(self, query, keep_text, count_lines):
        self.search = (re.compile(query.grep_pattern, get_regex_flags(query.modifiers)).search
                       if query.grep_pattern else None)
        self.blocks = _ConfigBlocks() if self.search and query.use_blocks else None
        self.matched_children_only = '@' in query.modifiers
        self.trim_matches = '%' in query.modifiers
        self.selected = _SelectedText(keep_text, count_lines, '\n\n' if self.blocks else '\n')

    def _add_block(self, block):
        if block:
            lines = _select_config_block(block, self.search, self.matched_children_only, self.trim_matches)
            if lines is not None:
                self.selected.add('\n'.join(lines))

    def add_line(self, line, repeats=1):
        if self.blocks is not None:
            # Repeated whitespace separators close a block only once.
            self._add_block(self.blocks.push(line))
        elif self.search:
            match = self.search(line)
            if match:
                self.selected.add(match.group(0) if self.trim_matches else line, repeats)
        else:
            self.selected.add(line, repeats)

    def finish(self):
        if self.blocks is not None:
            self._add_block(self.blocks.finish())
        if self.search and not self.selected.has_text:
            return None
        return self.selected


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
    """Parse and validate a -q query: [modifiers:]command[:pattern]."""
    return _parse_query_spec(query_string, use_blocks=False)


def parse_query_block(query_string):
    """Parse and validate a -Q query: [modifiers:]command[:pattern]."""
    return _parse_query_spec(query_string, use_blocks=True)


def _parse_query_spec(query_string, *, use_blocks):
    """Apply shared query rules and check patterns before any capture is read."""
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

        if not use_blocks and '@' in modifier_text:
            raise ValueError(f"Modifier '@' is only supported with -Q in query '{query_string}'.")

        allowed_modifiers = DEFINED_Q_BLOCK_MODIFIERS if use_blocks else DEFINED_Q_MODIFIERS
        unknown_modifiers = [char for char in modifier_text if char not in allowed_modifiers]
        if unknown_modifiers:
            raise ValueError(
                f"Unknown modifier(s) '{''.join(unknown_modifiers)}' in query '{query_string}'."
            )

        modifiers = frozenset(modifier_text)
        command, grep_pattern = parse_query(remainder)

    if not command:
        raise ValueError(f"Missing command in query '{query_string}'.")

    if modifiers.intersection({'%', '@', '/'}) and not grep_pattern:
        raise ValueError(f"Modifier(s) require a grep pattern in query '{query_string}'.")

    if grep_pattern:
        try:
            re.compile(grep_pattern, get_regex_flags(modifiers))
        except (re.error, ValueError, OverflowError, RecursionError) as error:
            raise ValueError(f"Invalid pattern in query {query_string!r}: {error}") from None

    return QuerySpec(command=command, grep_pattern=grep_pattern, use_blocks=use_blocks, modifiers=modifiers)


class _QueryAction(argparse.Action):
    """Collect validated queries and report tokens in argparse's encounter order."""
    def __init__(self, *args, use_blocks=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_blocks = use_blocks

    def __call__(self, parser, namespace, values, option_string=None):
        try:
            query = _parse_query_spec(values, use_blocks=self.use_blocks)
        except ValueError as error:
            raise argparse.ArgumentError(self, str(error)) from None

        if getattr(namespace, self.dest, None) is None:
            setattr(namespace, self.dest, [])
            namespace.query_arguments = []
        getattr(namespace, self.dest).append(query)
        namespace.query_arguments.extend(('-Q' if self.use_blocks else '-q', values))


def process_query(file_path, query_spec, *, content=None):
    """
    Return a QueryResult after extracting, filtering, and applying modifiers.

    Supplied content uses the buffered compatibility path; otherwise stream the
    file. A missing command or empty selection is NO_MATCH, never report text.
    """
    if content is None:
        return get_query_results(file_path, [query_spec])[0]
    if '+' in query_spec.modifiers:
        command_matches = extract_commands(file_path, query_spec.command, content=content)
        if isinstance(command_matches, tuple):
            return QueryResult(QueryStatus.NO_MATCH)
    else:
        command_header, command_output = extract_command(file_path, query_spec.command, content=content)

        if command_header is None:
            return QueryResult(QueryStatus.NO_MATCH)

        command_matches = [CommandMatch(header=command_header, output=command_output)]

    processed_outputs = []
    for command_match in command_matches:
        rendered_piece = _render_command_match(command_match, query_spec)
        if rendered_piece is not None:
            processed_outputs.append(rendered_piece)
    return QueryResult.from_output(_finish_query_output(processed_outputs, query_spec))


def _render_command_match(command_match, query_spec):
    """Filter one completed command block, independently of file reading."""
    command_output = command_match.output
    regex_flags = get_regex_flags(query_spec.modifiers)
    if query_spec.grep_pattern:
        if query_spec.use_blocks:
            if '@' in query_spec.modifiers or '%' in query_spec.modifiers:
                rendered_piece = grep_output_with_blocks_selective(
                    command_output,
                    query_spec.grep_pattern,
                    matched_children_only='@' in query_spec.modifiers,
                    trim_matches='%' in query_spec.modifiers,
                    regex_flags=regex_flags,
                )
            else:
                rendered_piece = grep_output_with_blocks(
                    command_output,
                    query_spec.grep_pattern,
                    regex_flags=regex_flags,
                )
        elif '%' in query_spec.modifiers:
            rendered_piece = grep_output_matches_only(command_output, query_spec.grep_pattern, regex_flags)
        else:
            rendered_piece = grep_output(command_output, query_spec.grep_pattern, regex_flags)
        if not rendered_piece:
            return None
    else:
        rendered_piece = command_output

    if '~' in query_spec.modifiers:
        matched_command = _render_matched_command_header(command_match.header)
        rendered_piece = f"{matched_command}\n{rendered_piece}" if rendered_piece else matched_command
    return rendered_piece


def _finish_query_output(processed_outputs, query_spec):
    """Combine rendered matches, then apply the final count modifier."""
    rendered_output = '\n\n'.join(processed_outputs)

    if '#' in query_spec.modifiers:
        return count_non_empty_lines(rendered_output)

    return rendered_output


def get_query_results(file_path, all_queries):
    """Return typed outcomes in query order, reading each capture only once."""
    if not all_queries:
        return []

    pieces = [[] for _ in all_queries]
    counts = [0 for _ in all_queries]
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as stream:
            # Preserve literal multiline command prefixes on the buffered path.
            if any('\n' in query.command for query in all_queries):
                content = stream.read()
                return [process_query(file_path, query, content=content) for query in all_queries]
            for order, index, output, count in _iter_selected_commands(stream, all_queries):
                if '#' in all_queries[index].modifiers:
                    counts[index] += count
                else:
                    pieces[index].append((order, output))
    except OSError as error:
        # Discard partial selections if a required read fails anywhere in the file.
        return [QueryResult(QueryStatus.READ_ERROR, error=str(error)) for _ in all_queries]

    # Blocks from different prompts can overlap and complete out of order. Keep
    # repeated results in command encounter order, just as the original parser did.
    results = []
    for index, query in enumerate(all_queries):
        if '#' in query.modifiers:
            output = str(counts[index]) if counts[index] else ''
        else:
            output = '\n\n'.join(piece for _, piece in sorted(pieces[index]))
        results.append(QueryResult.from_output(output))
    return results


def _file_is_selected(query_results, and_mode):
    """Use the same eligibility rule for normal, raw, and notes output."""
    return (all(result.status is not QueryStatus.READ_ERROR for result in query_results)
            and (not and_mode or all(result.status is QueryStatus.MATCH for result in query_results)))


def get_file_output(file_path, args, all_queries, *, query_results=None):
    """Return notes content, or None for an excluded file (distinct from empty)."""
    if query_results is None:
        query_results = get_query_results(file_path, all_queries)
    if not _file_is_selected(query_results, args.and_mode):
        return None
    return '\n\n'.join(result.output for result in query_results)


def _report_read_error(file_path, query_results, *, after_progress=False):
    """Report a file's read failure once, regardless of its query count."""
    for result in query_results:
        if result.status is QueryStatus.READ_ERROR:
            if after_progress:
                print(file=sys.stderr)
            print(f"Error reading file '{file_path}': {result.error}", file=sys.stderr)
            return True
    return False


def print_unique_banner(index, total, match_count):
    """Print a banner for unique output in notes mode"""
    match_text = f"matched {match_count} time" if match_count == 1 else f"matched {match_count} times"
    banner_text = f"<<< Unique Output {index} of {total} | {match_text} >>>"
    padding = (70 - len(banner_text)) // 2
    dashes = "-" * padding
    print(f"\n{dashes}{banner_text}{dashes}\n")


def collect_notes(output_groups):
    """Annotate each group; EOF and /done finish, while Ctrl-C propagates to cancel."""
    notes_collected = []
    for index, (output, filenames) in enumerate(output_groups.items(), 1):
        print_unique_banner(index, len(output_groups), len(filenames))
        print(output if output.strip() else '(empty)')
        while True:
            try:
                note = input('Notes: ')
            except EOFError:
                return notes_collected
            command = note.strip()
            if command.startswith('//'):
                # Remove one leading slash, preserving the rest of the note verbatim.
                start = len(note) - len(note.lstrip())
                note = note[:start] + note[start + 1:]
            elif command.startswith('/'):
                if command.lower() == '/done':
                    return notes_collected
                if command.lower() == '/help':
                    print(NOTES_HELP)
                else:
                    print(f"Unknown notes command {command!r}. Use /help for controls.", file=sys.stderr)
                continue
            if note.strip():
                notes_collected.extend((filename, note) for filename in filenames)
            break
    return notes_collected


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


def build_notes_command_summary(args):
    """Build a reproducible summary from parsed options, excluding file paths."""
    retained_tokens = ["showparse"]
    if args.and_mode:
        retained_tokens.append('-A')
    if args.raw:
        retained_tokens.append('-r')
    retained_tokens.extend(args.query_arguments)
    return shlex.join(retained_tokens)


@dataclass(frozen=True)
class ReportDestination:
    """The destination and input identities checked before collecting notes."""
    path: str
    target: str
    parent_identity: tuple[int, int]
    existing: os.stat_result | None
    inputs: tuple[str, ...]
    input_identities: frozenset[tuple[int, int]]


def _file_identity(info):
    return info.st_dev, info.st_ino


def _report_fingerprint(info):
    """Ignore access time so simply reading a report does not invalidate consent."""
    if info is None:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode)


def _inspect_report_path(path):
    """Resolve existing symlinks; reject dangling links and non-regular targets."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        parent = os.path.realpath(os.path.dirname(path), strict=True)
        target = os.path.join(parent, os.path.basename(path))
        existing = None
    else:
        target = os.path.realpath(path, strict=True)
        existing = os.stat(target)
        if not stat.S_ISREG(existing.st_mode):
            raise ValueError(f"Report destination '{path}' is not a regular file.")
        parent = os.path.dirname(target)
    parent_info = os.stat(parent)
    if not stat.S_ISDIR(parent_info.st_mode):
        raise ValueError(f"Report parent '{parent}' is not a directory.")
    return target, _file_identity(parent_info), existing


def _check_report_inputs(target, existing, inputs, original_identities):
    """Protect original inputs and whatever the input paths currently refer to."""
    identities = set(original_identities)
    for capture in inputs:
        if os.path.realpath(capture) == target:
            raise ValueError(f"Report destination refers to input capture '{capture}'. Choose a separate report path.")
        try:
            identities.add(_file_identity(os.stat(capture)))
        except FileNotFoundError:
            pass  # Missing inputs are diagnosed during capture processing.
        except OSError as error:
            raise ValueError(f"Cannot verify report protection for input '{capture}': {error}") from None
    if existing is not None and _file_identity(existing) in identities:
        raise ValueError(f"Report destination '{target}' refers to an input capture. Choose a separate report path.")
    return frozenset(identities)


def _check_report_destination(destination):
    target, parent_identity, existing = _inspect_report_path(destination.path)
    _check_report_inputs(target, existing, destination.inputs, destination.input_identities)
    if (target != destination.target or parent_identity != destination.parent_identity
            or _report_fingerprint(existing) != _report_fingerprint(destination.existing)):
        raise ValueError(f"Report destination '{destination.path}' changed during this run; refusing to replace it.")


def prepare_report_destination(output_file_path, input_files):
    """Check the destination and obtain any overwrite consent before parsing."""
    if os.name != 'posix':
        raise ValueError("Safe report saving requires a POSIX system such as Linux; use -n without -o to print notes instead.")
    # Do not normalize '..' before resolving symlinks: that can change its meaning.
    path = os.path.join(os.getcwd(), os.fspath(output_file_path))
    inputs = tuple(os.path.join(os.getcwd(), os.fspath(capture)) for capture in input_files)
    target, parent_identity, existing = _inspect_report_path(path)
    identities = _check_report_inputs(target, existing, inputs, ())
    destination = ReportDestination(path, target, parent_identity, existing, inputs, identities)
    if existing is not None:
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            raise ValueError(f"Report already exists: '{path}'. Overwrite confirmation requires a terminal; choose a new report path.")
        print(f"Report already exists: {path}", file=sys.stderr)
        if target != path:
            print(f"Resolved destination: {target}", file=sys.stderr)
        while True:
            print("Replace it when the new report is ready? [y/N] ", end='', file=sys.stderr, flush=True)
            try:
                answer = input().strip().lower()
            except EOFError:
                answer = ''
            if answer in ('y', 'yes'):
                break
            if answer in ('', 'n', 'no'):
                raise ValueError("Report replacement canceled; the existing file was left untouched.")
            print("Please answer y or n (Enter means no).", file=sys.stderr)
        _check_report_destination(destination)
    return destination


def save_notes_report(destination, report_text):
    """Publish a complete UTF-8 report without truncating an existing file.

    Directory-relative operations keep publication anchored if a parent symlink
    changes. New destinations use an exclusive hard link; approved replacements
    use an atomic rename after checking the destination again.
    """
    _check_report_destination(destination)
    parent, filename = os.path.split(destination.target)
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    temporary_name = None
    try:
        if _file_identity(os.fstat(directory_fd)) != destination.parent_identity:
            raise ValueError(f"Report parent '{parent}' changed during this run.")
        candidate = f'.showparse-notes-{os.urandom(12).hex()}.tmp'
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
        temporary_name = candidate
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output_file:
            output_file.write(report_text)
            output_file.flush()
            if destination.existing is not None:
                os.fchmod(output_file.fileno(), stat.S_IMODE(destination.existing.st_mode) & 0o777)
            os.fsync(output_file.fileno())
        _check_report_destination(destination)
        if destination.existing is None:
            # Fails if any entry appeared after the last check; never clobber it.
            os.link(temporary_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        else:
            os.replace(temporary_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass  # Atomic replacement already moved the temporary file.
            except OSError as error:
                print(f"Warning: Could not remove temporary report '{parent}/{temporary_name}': {error}", file=sys.stderr)
        os.close(directory_fd)


def print_notes_progress(completed, total):
    """Render notes-mode phase-1 progress to stderr on a single live line."""
    print(f"\r({completed}/{total})", end="", file=sys.stderr, flush=True)


def exit_quietly_for_broken_pipe(exit_status=0):
    """Exit cleanly when stdout is closed by a downstream pager or pipe consumer."""
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        raise SystemExit(exit_status)

    try:
        stdout_fd = sys.stdout.fileno()
    except (AttributeError, OSError):
        os.close(devnull_fd)
        raise SystemExit(exit_status)

    try:
        os.dup2(devnull_fd, stdout_fd)
    except OSError:
        pass
    finally:
        os.close(devnull_fd)

    raise SystemExit(exit_status)


def _iter_text_lines(text):
    """Split on literal newlines without allocating a list of all output lines."""
    start = 0
    while True:
        end = text.find('\n', start)
        if end < 0:
            yield text[start:]
            return
        yield text[start:end]
        start = end + 1


def iter_raw_output(filename, query_results):
    """Yield filename-prefixed lines without copying the whole rendered report."""
    for output in query_results:
        for line in _iter_text_lines(output):
            if line.strip():
                yield f"{filename}:{line}\n"


def build_raw_output(filename, query_results):
    """Buffered rendering helper retained for callers that need one string."""
    return ''.join(iter_raw_output(filename, query_results))


def iter_normal_output(file_path, query_results, show_banner=True, use_color=True):
    """Yield a banner, results, and separators without copying all results."""
    if show_banner:
        basename = os.path.basename(file_path)
        banner_text = f"<<< {basename} >>>"
        padding = (70 - len(banner_text)) // 2
        dashes = "-" * padding
        banner_line = f"{dashes}{banner_text}{dashes}"
        if use_color:
            yield f"\n\033[33m{banner_line}\033[0m\n\n"
        else:
            yield f"\n{banner_line}\n\n"

    for i, output in enumerate(query_results):
        yield output
        if i < len(query_results) - 1:
            yield "\n\n----------------------------------------\n\n"

    yield "\n"


def build_normal_output(file_path, query_results, show_banner=True, use_color=True):
    """Buffered rendering helper retained for callers that need one string."""
    return ''.join(iter_normal_output(file_path, query_results, show_banner, use_color))


def build_argument_parser():
    """Describe the CLI in one place, separate from execution."""
    parser = argparse.ArgumentParser(
        description='Extract command output from network device collection files (prompt-based parsing)',
        epilog='''Modifiers:
  %    matched text only (-q, -Q)
  ~    show matched command first (-q, -Q)
  #    count non-empty rendered lines (-q, -Q)
  /    case-sensitive pattern matching (-q, -Q)
  +    all matching commands for this query (-q, -Q)
  @    parent + matched child lines only (-Q only)

Examples:
  showparse -q "show version" *.dat
  showparse -q "~:show run:username" *.dat
  showparse -q "#:show version" *.dat
  showparse -Q "@%:show run:switchport mode access|shutdown" *.dat

Invalid queries or patterns exit with status 2 before captures are read.''' + '\n\n' + NOTES_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        '-q', '--query',
        action=_QueryAction,
        dest='queries',
        metavar='QUERY',
        help='Query in format "[modifiers:]command[:pattern]" (can be repeated)'
    )
    parser.add_argument(
        '-Q', '--query-block',
        action=_QueryAction,
        dest='queries',
        use_blocks=True,
        metavar='QUERY',
        help='Block query in format "[modifiers:]command[:pattern]" (can be repeated)'
    )
    
    # Query logic mode
    parser.add_argument(
        '-A', '--and',
        action='store_true',
        dest='and_mode',
        help='AND mode: every query must produce non-empty selected content, including in notes mode.'
    )
    
    # Output mode
    parser.add_argument(
        '-r', '--raw',
        action='store_true',
        help='Raw output mode: no banners, prefix each line with filename (grep-style)'
    )
    parser.add_argument(
        '--no-color',
        action='store_true',
        help='Normal mode only: print the per-file filename banner without ANSI color.'
    )
    parser.add_argument(
        '--no-banner',
        action='store_true',
        help='Normal mode only: suppress the per-file filename banner.'
    )
    parser.add_argument(
        '-n', '--notes',
        action='store_true',
        help='Group identical results for annotation. See notes controls below.'
    )
    parser.add_argument(
        '-o', '--output-file',
        metavar='PATH',
        help='Save the notes report; existing reports require terminal confirmation. Input captures are protected. Requires -n.'
    )
    
    # Files
    parser.add_argument(
        'files',
        nargs='+',
        help='File(s) to parse. Supports glob patterns like *.dat'
    )
    
    return parser


def parse_arguments(argv=None):
    """Parse ordered queries and reject incompatible modes before file access."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    # Validate: can't use both -r and -n
    if args.raw and args.notes:
        parser.error("Cannot use both -r and -n. Raw mode and notes mode are mutually exclusive.")
    if args.output_file and not args.notes:
        parser.error("-o/--output-file can only be used with -n/--notes.")
    
    if not args.queries:
        parser.error("At least one -q/--query or -Q/--query-block is required")

    return args


def run_notes_mode(files, args, report_destination):
    """Group selected output, annotate it, then publish completed notes."""
    had_read_errors = False
    try:
        # Phase 1: Collect all outputs and group by unique content
        output_groups = {}  # {output_content: [filenames]}
        total_files = len(files)
        for completed, file_path in enumerate(sorted(files), 1):
            filename = os.path.basename(file_path)
            query_results = get_query_results(file_path, args.queries)
            if _report_read_error(file_path, query_results, after_progress=completed > 1):
                had_read_errors = True
            else:
                output = get_file_output(file_path, args, args.queries, query_results=query_results)
                if output is not None:
                    output_groups.setdefault(output, []).append(filename)
            print_notes_progress(completed, total_files)

        print(file=sys.stderr)

        # Phase 2: Present each unique output and collect notes.
        try:
            notes_collected = collect_notes(output_groups)
        except KeyboardInterrupt:
            print()  # Leave the shell on a fresh line; never save canceled annotations.
            return 130

        # Phase 3: Print notes report
        if notes_collected:
            command_summary = build_notes_command_summary(args)
            report_text = build_notes_report(notes_collected, command_summary)
            if args.output_file:
                try:
                    save_notes_report(report_destination, report_text)
                except (OSError, ValueError, KeyboardInterrupt) as error:
                    reason = 'Save interrupted' if isinstance(error, KeyboardInterrupt) else str(error)
                    failure_status = 130 if isinstance(error, KeyboardInterrupt) else 1
                    print(f"Could not save notes report to '{args.output_file}': {reason}", file=sys.stderr)
                    print("Collected notes are printed below for recovery.", file=sys.stderr)
                    try:
                        print(report_text)
                    except BrokenPipeError:
                        exit_quietly_for_broken_pipe(failure_status)
                    return failure_status
            else:
                print(report_text)
    except BrokenPipeError:
        exit_quietly_for_broken_pipe(int(had_read_errors))
    return int(had_read_errors)


def run_output_mode(files, args):
    """Process each capture and render its selected queries in argument order."""
    had_read_errors = False
    try:
        wrote_normal_output = False
        for file_path in sorted(files):
            filename = os.path.basename(file_path)

            query_results = get_query_results(file_path, args.queries)
            if _report_read_error(file_path, query_results):
                had_read_errors = True
                continue
            if not _file_is_selected(query_results, args.and_mode):
                continue

            outputs = [result.output for result in query_results]
            if args.raw:
                sys.stdout.writelines(iter_raw_output(filename, outputs))
            else:
                if args.no_banner and wrote_normal_output:
                    sys.stdout.write("\n")
                sys.stdout.writelines(
                    iter_normal_output(
                        file_path,
                        outputs,
                        show_banner=not args.no_banner,
                        use_color=not args.no_color,
                    )
                )
                wrote_normal_output = True
            del outputs, query_results  # Release this result before reading the next capture.

        # Add a newline at the end for cleaner output (not in raw mode)
        if wrote_normal_output and not args.no_banner:
            sys.stdout.write("\n")
    except BrokenPipeError:
        exit_quietly_for_broken_pipe(int(had_read_errors))
    except KeyboardInterrupt:
        print()
        sys.exit(130)
    return int(had_read_errors)


def _run_cli():
    args = parse_arguments()
    files = expand_file_patterns(args.files)
    if not files:
        print("Error: No files found matching the specified pattern(s).", file=sys.stderr)
        return 1

    report_destination = None
    if args.notes and args.output_file:
        try:
            report_destination = prepare_report_destination(args.output_file, files)
        except (OSError, ValueError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nReport replacement canceled.", file=sys.stderr)
            return 130

    if args.notes:
        return run_notes_mode(files, args, report_destination)
    return run_output_mode(files, args)


def main():
    """Handle terminal shutdown, including errors delayed until stdout flushes."""
    status = 0
    try:
        try:
            status = _run_cli()
        except SystemExit as error:
            # argparse help and usage errors exit before returning to this wrapper.
            status = error.code if isinstance(error.code, int) else int(error.code is not None)
            sys.stdout.flush()
            raise
        sys.stdout.flush()
    except BrokenPipeError:
        exit_quietly_for_broken_pipe(status)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    return status


if __name__ == '__main__':
    sys.exit(main())
