# ShowParse

ShowParse extracts command output from collected network-device SSH transcripts,
filters it, and helps produce notes across many captures. Its input is a cleaned
session log containing device prompts, echoed commands, and their output.

The tool is one standalone script, [showparse.py](showparse.py), with usage
documented here. Tests and development tools support the parser. It accepts
command transcripts from network collectors that produce the format described
below; command names are not restricted to those beginning with `show`.

## Project layout and scope

```text
showparse.py        Main standalone parser
README.md          Usage, limitations, and development documentation
tests/             Behavior and regression tests
dev/               Synthetic-capture generator and benchmark runners
.gitignore         Excludes generated data, measurements, and Python caches
test-data/         Locally generated captures, excluded from Git
benchmark-results/ Local measurements, excluded from Git
```

Only the script is needed to run ShowParse. The tests, generators, and benchmarks
are optional development tools; generated captures and measurements stay local.

Collection and parsing remain separate projects. ShowParse reads saved captures;
it does not open device sessions or import the collection project's code.

## Requirements and input

This is a **Linux command-line tool** requiring **Python 3.10 or newer**, with
**no third-party packages**. Run the script directly; no installation is required:

```bash
python3 showparse.py --help
```

A typical input looks like this:

```text
Router1#show version
Cisco IOS Software ...
Router1#show running-config
hostname Router1
!
interface GigabitEthernet0/1
 description LAN
 shutdown
!
Router1#
```

Login banners and other session text may surround these sections. File extensions
are unrestricted: `.txt`, `.dat`, and `.show` paths can all be used. Input is read
as UTF-8; invalid bytes become replacement characters rather than stopping the
parse. Ordinary Unix and Windows line endings are supported.

Current extraction rules:

- The prompt starts at the beginning of a line, contains no whitespace, and ends
  in `#`, `>`, `$`, or `%`. Zero or one literal space may separate it from the
  echoed command.
- The command query is a literal, case-insensitive prefix. For example, `show run`
  matches `show running-config`. Device command abbreviations are not expanded.
- Output ends at the next line beginning with the same prompt, or at end of file.
- The first matching command is used by default; the `+` modifier selects all
  matching occurrences.
- ANSI formatting, backspaces, and terminal cursor movement are not cleaned by
  ShowParse. Supply a cleaned transcript for reliable matching.

For ordinary single-line command queries, all `-q` and `-Q` options share one
forward pass through each file. Reading stops once every first-match query has a
complete command block. Any `+` query, a missing command, or an unfinished block
keeps the scan going to end of file. An output-filter miss does not cause a
first-match query to search later command occurrences.

The reader processes chunks of 65,536 characters, extending each to a line
boundary. Line filters run as text arrives; configuration filters finish one
parent/child block at a time. Queries with the same selection rules share their
filter state. Count queries accumulate totals without retaining selected text
unless another query also needs that text. Unfiltered queries process whole
chunks. Results retain query order and repeated commands retain encounter order,
even when blocks from different prompts overlap. UTF-8 replacement, whitespace
trimming, and prompt matching rules are preserved.

Raw and normal reporting write output in pieces rather than constructing another
complete formatted report. Memory still depends on the longest line, current
configuration blocks, pending whitespace, and retained results. A giant
unfiltered result, unfiltered `+` output, or many unique notes results can still
consume substantial memory. Long runs of identical blank lines are compressed
while waiting to determine whether they belong to the command's trailing
whitespace. Literal multiline command prefixes and buffered helper APIs retain
their compatibility path and its larger memory requirements.

## Usage

Run these examples from the repository directory, replacing `captures/*.txt`
with your own capture paths. To generate a small sample collection, see the
baseline instructions below.

```bash
# Extract a command's output across files.
python3 showparse.py --no-color -q 'show version' captures/*.txt

# Filter output lines using a regular expression.
python3 showparse.py -q 'show run:hostname|username|logging host' captures/*.txt

# Include the matched command before each repeated result.
python3 showparse.py -q '~+:show version' captures/*.txt

# Return configuration blocks containing an exact shutdown line.
python3 showparse.py -Q 'show run:^ shutdown$' captures/*.txt

# Return each matching block's parent and matching child lines.
python3 showparse.py -Q '@:show run:^ shutdown$' captures/*.txt

# Return a matched substring, or count the rendered non-empty lines.
python3 showparse.py -q '%:show logging:Trap logging: level \w+' captures/*.txt
python3 showparse.py -q '#:show version' captures/*.txt
```

Query syntax is `[modifiers:]command[:pattern]`. Repeat `-q` and `-Q` to combine
queries in their command-line order. Patterns use Python regular expressions and
are case-insensitive by default. Quote queries, especially those containing `#`,
and prefer a space between each query option and its value for readability.

Separated and attached values are equivalent, including when mixing query types:

```bash
-q 'show version:Model'
-q'show version:Model'
--query 'show version:Model'
--query='show version:Model'
```

The same forms work for `-Q`/`--query-block`; short options also accept `=`, such
as `-Q='@:show run:shutdown'`. Queries retain their order with combined short
flags (for example, `-rAq'show version:Model'`) and unambiguous long-option
abbreviations. Prefer complete option names in saved commands. Use `--` before
filenames beginning with `-`; those filenames are never interpreted as queries.

| Option | Purpose |
| --- | --- |
| `-q` | Extract command output and optionally filter matching lines |
| `-Q` | Filter configuration blocks using unindented parents and indented children |
| `-A` | Include a file only when every query produces selected content, in all output modes |
| `-r` | Prefix non-empty output lines with the input basename, without banners |
| `--no-color` | Disable color in normal-mode filename banners |
| `--no-banner` | Suppress normal-mode filename banners |
| `-n` | Group identical results for interactive annotation |
| `-o PATH` | Save the final annotation report; requires `-n` |

Block filtering uses indentation, blank lines, and `!` separators. It is intended
for configuration text in that style, not a vendor-specific configuration grammar.

| Modifier | Effect |
| --- | --- |
| `%` | With `-q`, return the first matched substring per line; with `-Q`, trim matching lines while retaining selected block context |
| `~` | Show the matched command before its result |
| `#` | Count non-empty rendered lines after other modifiers, including any `~` command lines |
| `+` | Process every matching command occurrence |
| `@` | With `-Q`, retain the parent and matching children; a parent match still selects the full block |
| `/` | Make the output pattern case-sensitive; command matching remains case-insensitive |

### Query validation

Query syntax and every output-filter pattern are checked while processing the
command line, before expanding input paths or opening captures and reports. Both
`-q` and `-Q` require a non-empty command. Unknown modifiers, modifiers missing a
required pattern, and malformed regex patterns produce an explanation on stderr
and exit with status **2**, without a Python traceback or partial results.

For example, `-q 'show version:['` reports an invalid pattern and an unterminated
character set at position 0. This also fails immediately when the command is
absent, the capture is empty, or a later query is invalid after valid earlier
queries. The command portion remains a literal prefix; only the output filter
is interpreted as a regex. A syntactically valid query that finds nothing is
still a normal no-match result, not a usage error.

Existing optional-pattern behavior is retained: `-q 'show version'` and
`-Q 'show run'` return the command output without filtering, and an empty trailing
pattern is treated as omitted. `%` and `/` require a pattern; `@` also requires a
pattern and is supported only with `-Q`.

### Query outcomes and `-A`

Every query now returns one of three internal outcomes, separately from its text:

| Outcome | Meaning | Effect on `-A` |
| --- | --- | --- |
| Match | The selected result contains non-whitespace content after filtering and modifiers | Satisfies this query |
| No match | The command is absent, its output is empty, or filtering selects no content | Excludes the file |
| Read error | Opening or reading the capture failed | Excludes the file and reports the failure |

For example, `-A -q 'show version' -q 'show logging'` includes a capture only if
both queries produce content. A generated "command not found" message no longer
counts as a match or appears in extracted results. Real captured text that happens
to resemble an error message remains valid content.

Without `-A`, queries with no selected content contribute empty results; other
queries still display normally. Normal mode retains its banners and query
separators, raw mode emits no lines for an empty result, and notes mode allows
empty results to be annotated. With `-A`, rejected files produce no normal/raw
output and do not enter notes groups.

Existing modifier behavior is preserved: `+` combines selected content across
occurrences; a first-match query does not try later occurrences after a filter
miss. With no output filter, `~` explicitly selects the matched command line, so
that line can satisfy `-A` even if the command has no body. `#` counts rendered
non-empty lines, including any selected command line; an empty selection stays
empty rather than becoming a count of `0`.

Read errors go to stderr once per failed file, with its path. That file's partial
results are discarded and other files continue processing. A run that encounters
a capture read error exits with status **1**, including when valid notes are
saved. Normal completion exits with **0**, even if no query matches. File-read
checks cover only the portion needed for the queries: early stopping does not
verify the unread remainder of a capture. Unmatched file patterns still warn and
are skipped; if no input files remain, the run exits with status 1.

### Notes mode

```bash
python3 showparse.py -q 'show version' -n captures/*.txt
python3 showparse.py -q 'show version' -n -o review-notes.txt captures/*.txt
```

Notes mode displays each unique combined result once and applies your note to all
files with that result. Annotation shows the group banner, match count, captured
output, and a plain `Notes:` prompt. Controls appear only in `--help` or when you
enter `/help`; there are no startup instructions or per-note acknowledgments.
A live `(completed/total)` counter on stderr shows progress while results are grouped.

| Input at `Notes:` | Behavior |
| --- | --- |
| Ordinary text | Record a note for every file in this group, then advance |
| Empty Enter | Skip this group; whitespace-only lines also skip |
| `/done` | Finish early using completed notes |
| `/help` | Display controls and return to the same group |
| Ctrl-C | Cancel annotation, discard session notes, and leave report files untouched; exit 130 |
| `//text` | Record `/text`; one initial slash is removed |

**`q` and `Q` are now ordinary notes.** Use `/done` to finish early. Slash commands
occupy a whole line and are case-insensitive, ignoring surrounding whitespace.
An unknown slash command gives a short error and stays on the same group. To
record a note beginning with a slash, double that slash: `//done` records `/done`,
and `//etc/example` records `/etc/example`. Other note text and spacing are retained.

Reaching the last group or end of input (including Ctrl-D on an empty terminal
line) also finishes using completed notes. With `-o`, finishing saves quietly;
without `-o`, the actual report is printed. If there are no notes, nothing is
written or announced. Ctrl-C during annotation returns quietly to the shell.
The final report contains the query summary and `filename:note` entries; it does
not include the extracted command output. `-r` and `-n` are mutually exclusive.
The summary uses canonical `-q`/`-Q` options in the supplied query order, including
queries originally written in attached or abbreviated forms. `-A` is retained;
input paths, interactive mode, and the report destination are omitted.

With `-o`, the destination is checked **before captures are parsed or notes are
requested**:

- Input captures are always protected, including symlinks, hard links, and inputs
  selected by a glob. A destination that refers to an input is refused without an
  overwrite prompt. Keep reports outside your capture glob when appropriate.
- A new destination proceeds normally. Its parent directory must already exist.
- An existing report requires confirmation from a terminal. The full destination
  is shown, along with its resolved target when different:

  ```text
  Report already exists: /path/to/review-notes.txt
  Replace it when the new report is ready? [y/N]
  ```

Only `y` or `yes` confirms (case-insensitive); Enter, `n`, `no`, or end of input
cancels before parsing. Other answers prompt again. Both stdin and stderr must
be terminals so piped notes cannot accidentally confirm replacement and the
prompt is visible. Unattended runs must use a new report path. There is no force
option, and confirmation never overrides capture protection. Directories, special
files, dangling symlinks, and symlink loops are refused as report destinations.

Answering yes leaves the existing report untouched while you annotate. No notes
means no report is written. Before saving, ShowParse checks input aliases and the
destination again. A changed destination, modified report, or file that appeared
at a previously unused path stops the save. A complete UTF-8 report is prepared
and flushed in the destination directory before publication. Existing reports are
replaced atomically; new reports are published only if the destination is still
unused. An approved report symlink is retained and its resolved target is updated.

If saving fails or a destination change is detected, the completed report is
printed to stdout for recovery and an explanation goes to stderr. Refusal,
declined confirmation, and save failures exit with status 1; Ctrl-C exits with
130. Cancellation during annotation intentionally discards notes; interruption
during an attempted save still prints the completed report for recovery.
New reports have private permissions (`0600`);
replacements preserve ordinary permission bits. Replacement creates a new file
identity; other hard links to an old report retain their previous contents.

File report saving uses POSIX directory operations, hard links, and atomic rename,
and is tested on Debian. A filesystem that cannot perform these save operations
causes a save failure with the report printed for recovery. The destination checks
detect changes during a session;
they do not lock an existing report against other writers between the final check
and replacement.

## Current status and limitations

The original performance baseline was captured from commit `7e92fc6`; the current
parser adds shared file reading, streaming extraction, and explicit query outcomes
with consistent `-A` handling. Queries and patterns are validated before input is
read, and argument handling preserves query order across accepted option forms.
Report saves protect inputs and require confirmation before replacing an existing
report.
The existing parser has been used successfully in the field. Initial local checks
also extracted `show version`, `show running-config`, and `show clock` from all
five available cleaned captures in the related collection project, including
access through its latest `.show` symlinks. This is limited compatibility evidence,
not a completed integration test.

The modular collector provides cleaned UTF-8 transcripts. Its separate manual
collector preserves raw terminal bytes, so those captures may need cleanup before
parsing. Commands must be echoed into the transcript for ShowParse to find them.
End-of-file alone does not establish that a collection completed successfully.

Input assumptions and remaining limitations:

- Notes reports identify files by basename. Operators should make filenames
  unique across the selected collection, adding site prefixes or suffixes when
  needed; automatic disambiguation is outside the current scope.
- Prompt changes can include unrelated later output; prompt-like text can end a
  result too early. Keep the established boundary rules until a representative
  capture demonstrates a failure. Raw terminal formatting can prevent command recognition.
- Full-file scans and filtering large command outputs still take time. Notes mode
  holds all unique rendered result text in memory.

## Behavior and performance baseline

The small regression suite uses temporary synthetic captures and requires no
large dataset or third-party packages:

```bash
python3 -m unittest discover -s tests -v
```

All six original known-defect reproductions now pass as ordinary regression tests.
The suite covers query ordering, modifiers, configuration blocks, notes, UTF-8
replacement, line endings, arbitrary command names, and `.show` symlinks. It also
checks generator integrity and the benchmark's resource limits.
Streaming checks verify early stopping, full scans for missing or `+` queries,
shared blocks, read failures, overlapping prompts, and text-chunk boundaries.
Outcome checks cover `-A` across all output modes, missing commands, empty and
filtered selections, modifier interactions, error-like captured text, and
continuing with readable files after a read failure. Argument checks cover
malformed queries and regex patterns before file access, accepted option forms,
mixed query order, literal filenames after `--`, and reproducible notes summaries.
Report checks cover confirmation in a real terminal, piped input, protected input
aliases, changing destinations, failed writes, and recovery of collected notes.
Control checks cover quiet output, on-demand help, literal `q`/`Q`, slash escaping,
finishing versus canceling, and terminal interruption after completed annotations.
Additional checks cover closed pipes during writes and final flushing, cancellation
during notes preparation, configuration separators, all modifier combinations,
streamed versus buffered results, and bounded memory for large filtered commands.
The current suite has 112 passing tests and no expected failures. Passing these
checks does not remove the input assumptions and limitations above.

### Synthetic collections

[dev/generate_captures.py](dev/generate_captures.py) streams deterministic captures
to disk, using bounded memory. The data includes login text, Cisco-, Junos-, and
PAN-OS-style prompts and configurations, interface counters, routes, logs, and
diagnostics. Commands include `dir flash`, `request support information`, and
`debug dataplane internal statistics`, as well as `show` commands.

Each capture has repeated command sections, LF or CRLF line endings, unique
device identifiers, a relative `.show` symlink, and known result markers near the
start, middle, and end. Its manifest records file sizes, SHA-256 hashes, and
expected matches independently of the parser. Symlinks add no second copy of the
capture; select either the original files or the links to avoid double counting.

| Profile | Captures | Size per capture | Total capture bytes |
| --- | ---: | --- | ---: |
| `smoke` | 12 | 64 KiB | 768 KiB |
| `fleet` | 500 | 4 MiB | 1.953 GiB |
| `large` | 8 | 512 MiB | 4 GiB |
| `mixed` | 32 | 16, 64, 128, or 256 MiB | 3.625 GiB |
| `full` | 540 | All three larger profiles | 9.578 GiB (about 10.3 GB) |

These are allocated text files, not sparse placeholders or multiple links to the
same file. Bounded record windows repeat within large outputs: this provides
useful parsing workloads, but does not simulate the full diversity or compression
characteristics of production captures. Vendor formatting is illustrative, not a
validated device emulator. Invalid bytes and boundary edge cases live in the small
tests rather than being mixed into the performance datasets.

```bash
# Start small; the default profile is smoke.
python3 dev/generate_captures.py
python3 showparse.py -q 'show' test-data/smoke/*.show

# Inspect the larger allocation before generating it.
python3 dev/generate_captures.py --profile full --plan
python3 dev/generate_captures.py --profile full
```

Generation refuses to overwrite an existing profile. To make another copy, use
`--output PATH`; the defaults cap generated content under that output root at
12 GiB and retain at least 16 GiB of free disk space. `--budget-gib` can raise that
cap to at most 20 GiB. An interrupted generation leaves partial files in place
and writes no completed manifest; select a new output location or remove the
partial profile deliberately before retrying. Captures and measurements in the
default directories are excluded from Git.

### Measuring the parser

[dev/benchmark.py](dev/benchmark.py) runs six workloads sequentially: an early
command, a command at the end, multiple queries, all repeated `show` commands, a
nonmatching output pattern, and notes grouping. Results are checked against the
manifest; notes checks verify the expected groups and file counts. It saves wall
time, CPU time, peak resident memory, output hashes, parser and manifest hashes,
and environment details in a new JSON report after every workload.

```bash
python3 dev/benchmark.py test-data/smoke --repeat 3
python3 dev/benchmark.py test-data/fleet
python3 dev/benchmark.py test-data/large --limit-files 1
python3 dev/benchmark.py test-data/mixed

# Compare symlink inputs or select a workload.
python3 dev/benchmark.py test-data/fleet --via-links --workload multiple
```

The runner targets Linux and defaults to a 60-second wall-time limit and a
3 GiB address-space limit per parser process. Timeout, memory exhaustion, and
incorrect results are recorded as failures, not speed improvements. Limits may
be adjusted using `--timeout` and `--memory-mib`; run larger workloads explicitly
after reviewing the smaller results. Reports are created exclusively, so an
existing report cannot be overwritten.

OS disk-cache state is **uncontrolled** and may be warm immediately after
generation or an earlier run. The runner does not evict caches. Compare repeated
runs on the same dataset and environment; do not extrapolate these measurements
into a claim about parsing 500 separate 512 MiB captures. The fleet and large-file
profiles isolate those two scales without allocating 250 GiB.

Initial local measurements on October 2, 2026, used the unchanged parser on
Python 3.13.5, four CPUs, and an 8 GB Debian VM. These are single runs of the larger
profiles with uncontrolled cache state, not throughput guarantees:

| Input | Early command | Last command | All repeated matches |
| --- | ---: | ---: | ---: |
| 500 × 4 MiB | 3.38 s | 16.51 s | 50.22 s |
| One 512 MiB capture | 1.07 s | 4.41 s | 14.78 s |
| 32 mixed captures, 3.625 GiB total | 4.15 s | 30.20 s | Stopped at the 60 s limit |

The single 512 MiB capture peaked at approximately 1.51 GiB of resident memory.
All completed workloads matched their expected results; the mixed repeated-match
workload timed out. The large profile contains eight files, but this initial
benchmark selected one. Full per-workload results and dataset integrity checks
are saved locally under `benchmark-results/`.

The extraction update was validated in two stages: shared buffered reading with
offset searches, followed by streaming. On the same 512 MiB capture:

| Workload | Original | Stage 1 | Streaming |
| --- | ---: | ---: | ---: |
| Early command | 1.07 s | 0.91 s | 0.04 s |
| Two queries, early and late | 5.23 s | 4.47 s | 4.18 s |
| All repeated matches | 14.78 s | 12.85 s | 10.46 s |
| Peak resident memory across six workloads | 1,544 MiB | 1,543 MiB | 89 MiB |

Across 500 captures, early-command extraction changed from 3.38 to 0.10 seconds,
and repeated-match extraction from 50.22 to 40.42 seconds. The mixed repeated-match
workload completed in 76.09 seconds. Rerunning the original with the same extended
120-second limit took 104.98 seconds; the original 60-second timeout alone was
not a usable runtime comparison. This mixed workload still exceeds the runner's
default 60-second limit.

These are local single-run comparisons with uncontrolled cache state. The
streaming-stage snapshot passed all 24 benchmark workloads and 30 tests, with
the same six known expected failures. Another 36,000 query comparisons against the original
covered varied captures and chunk sizes; 66 comparisons on 11 existing collector
captures also matched. Snapshots of the original, stage-one, and streaming parser,
plus their reports and validation summaries, are retained in `benchmark-results/`.

After the query-outcome fix, all six workloads were rerun on both the smoke
collection and one 512 MiB capture. All 12 passed and their stdout hashes matched
the streaming-stage snapshot. On the large capture, early selection took 0.06 s,
two queries took 4.16 s, and repeated matches took 10.64 s with 88.5 MiB peak
resident memory. These single runs show similar performance, subject to the same
cache and timing limitations. The parser snapshot and reports are saved locally
as `benchmark-results/outcomes-*`; the intentional changes for missing commands,
`-A`, and read errors are covered by the regression suite.

After the argument-validation update, all six smoke workloads and the early and
two-query workloads on one 512 MiB capture passed, with stdout hashes matching
the query-outcome snapshot. The large-file runs took 0.04 s and 4.36 s, with peak
resident memory of 14.8 and 15.2 MiB respectively. These remain single-run
measurements with uncontrolled cache state. The updated snapshot, measurements,
and validation summary are saved locally as `benchmark-results/query-validation-*`.

The report-safety update passed all 76 regression tests, including confirmation
through a real terminal and injected save failures. All six smoke benchmark
workloads passed with stdout hashes matching the argument-validation snapshot;
peak resident memory was about 15.2 MiB. The final parser snapshot and validation
summary are saved locally as `benchmark-results/notes-safety-showparse.py` and
`benchmark-results/notes-safety-validation.json`, with the benchmark results in
`benchmark-results/notes-safety-smoke-final.json`.

The quiet notes-controls update passed all 89 tests, including cancellation in a
terminal after a completed annotation, and all six smoke workloads. The five
extraction workload outputs matched the preceding snapshot. Notes grouping stayed
the same; its completion message was intentionally removed. The current snapshot
and validation summary are saved as `benchmark-results/notes-controls-showparse.py`
and `benchmark-results/notes-controls-validation.json`, with measurements in
`benchmark-results/notes-controls-smoke.json`.

## Development plan

**Baseline foundation:** the small behavior suite, synthetic collections, and
bounded benchmark runner now provide a repeatable starting point. Preserve their
results before changing the parser, and extend them as new field cases arise.
Keep private field captures out of the repository.

Then proceed in five steps:

1. **Optimize extraction — implemented in two validated stages.** First share
   one file read across queries and search by offsets; then stream command blocks
   with early stopping. Preserve snapshots and measurements for each stage. Any
   further filtering or memory optimization should have its own measured baseline.
2. **Make query outcomes dependable — implemented.** Matches, empty
   selections, and read failures are distinct; `-A` now applies consistently in
   all modes, and read failures produce diagnostics and a failure exit status.
   Patterns and query arguments are now validated before reading captures, and
   one argument-handling path preserves query order and report summaries.
3. **Protect notes and reports — implemented.** Input aliases are
   protected, existing reports require confirmation, and saves recheck destinations
   and publish complete reports. Quiet controls distinguish `/done` (finish using
   completed notes) from Ctrl-C (cancel annotation without saving). Capture naming
   remains the operator's responsibility.
4. **Validate transcript boundaries with field evidence.** Keep current prompt
   matching behavior. If a cleaned capture demonstrates a boundary problem, add
   that case and review a focused fix before changing the supported input rules.
5. **Verify and document the result.** Repeat the functional and performance
   checks, validate the saved-capture workflow with the collection project, and
   update usage and limitations with measured results.

Keep changes small and reviewable. Preserve established output unless a specific
fix requires a documented change. Favor a standalone standard-library script;
add abstractions or dependencies only when they solve a demonstrated problem.

## Code-review improvement checklist

Work through these stages independently. Preserve command matching, modifier
semantics, output bytes, report protections, and quiet notes controls. Each stage
must pass its focused checks and the full regression suite before the next stage.

- [x] **CLI lifecycle:** catch closed pipes, including the final stdout flush;
  cancel notes preparation cleanly on Ctrl-C. Validate short and large piped
  output, failure exit statuses, and cancellation without report writes.
  The initial stage passed 93 tests. Final coverage also includes piped help and
  preserving a report-save failure when recovery output encounters a closed pipe.
- [x] **Shared configuration blocks:** use one parent/child block iterator for
  both filters; remove redundant sorting and child searches. Validate parent and
  child matches, separators, indentation, `%`/`@` combinations, and ordering.
  All 99 tests pass; 18,000 seeded comparisons match the review snapshot.
- [x] **Compiled patterns:** reuse compiled searches in filtering loops. Validate
  case sensitivity, inline flags, captures, zero-width matches, and invalid-query
  diagnostics; measure filtering CPU time against the review snapshot.
  All 100 tests pass. Five alternating runs of a 50,000-block fixture reduced
  median filtering CPU time from 0.124 to 0.056 seconds for line filtering,
  0.181 to 0.124 for full blocks, and 0.232 to 0.145 for selective trimmed blocks.
  These isolate filtering; they are not end-to-end capture speed guarantees.
- [x] **Code organization:** separate argument setup, notes mode, and ordinary
  reporting; remove unused banner code and stale comments. Retain buffered helper
  compatibility. Compare help, output, errors, and notes reports to the snapshot.
  All 100 tests pass; 39 CLI comparisons preserve help, diagnostics, query output,
  exit statuses, and notes reports byte for byte.
- [x] **Selected-output memory:** filter and count incrementally, avoid building
  a second complete raw report, and retain only necessary state. Validate large
  individual commands, multiple queries, repeated/overlapping commands, counts,
  whitespace, chunk boundaries, and `-A`; compare result hashes and peak memory.
  All 112 tests pass, including a selected-memory regression check. The final
  parser matches 28,800 seeded query comparisons against the pre-review snapshot
  and 39 CLI comparisons. All 24 existing dataset workloads and all eight new
  large-command workloads pass. The final snapshot and validation summary are
  `benchmark-results/review-final-showparse.py` and
  `benchmark-results/review-final-validation.json`.

Use bounded temporary data for the new workloads; do not enlarge the existing
multi-gigabyte collection. Save stage snapshots and measurements locally under
`benchmark-results/`. Record completed validation here. Any change to user-facing
semantics requires a separate decision rather than an incidental refactor.

The implementation remains one standalone script. To follow execution, start at
`main()` / `_run_cli()`, then `run_output_mode()` or `run_notes_mode()`.
`get_query_results()` manages the shared capture scan; `_ActiveCommand` preserves
command boundaries and whitespace; `_CommandSelection` applies filters; and
`_SelectedText` retains text or only counts. `_ConfigBlocks` supplies the same
parent/child rules to streamed and buffered filters. The older buffered extraction
and rendering helpers remain available for compatibility.

### Large selected-command benchmark

This separate benchmark covers the case where an individual command, rather than
just the surrounding capture, is large. It generates two temporary commands of
the requested size, verifies every output against independently computed counts
or hashes, and removes its captures afterward. Each parser process is limited to
1 GiB of address space and 60 seconds. The benchmark hashes large output without
loading it into the parent process. Results are created exclusively, so choose a
new output path for each run.

```bash
python3 dev/benchmark_selected.py --mib 64 --output benchmark-results/selected-new.json
```

The 64 MiB command measurements below compare the pre-review snapshot with the
completed implementation. Times are single local runs with uncontrolled cache and
scheduling effects, not throughput guarantees.

| Workload | Previous peak memory | Current peak memory | Previous time | Current time |
| --- | ---: | ---: | ---: | ---: |
| Count log lines | 207 MiB | 16 MiB | 0.73 s | 0.55 s |
| Log filter, no matching lines | 251 MiB | 16 MiB | 1.25 s | 1.13 s |
| Count matching log lines | 315 MiB | 16 MiB | 1.53 s | 1.63 s |
| Count full matching config blocks | 766 MiB | 16 MiB | 5.88 s | 5.11 s |
| Raw output of the entire log command | 332 MiB | 145 MiB | 0.79 s | 0.75 s |

All eight workloads produced the expected output. The main gain is much lower
working memory for filters and counts; some filtered workloads take slightly
longer. Full-output queries still retain their selected text. The local reports
are `benchmark-results/review-selected-before.json` and
`benchmark-results/review-selected-final.json`.

The existing smoke, 500-device fleet, 512 MiB single-capture, and mixed-size
profiles also passed all six workloads each. On the 512 MiB repeated-match case,
peak memory was 16.5 MiB and runtime was 9.97 seconds, compared with 88.5 MiB and
10.64 seconds in the earlier outcome-fix measurement. The mixed repeated-match
case completed in 73.76 seconds with 17.1 MiB peak memory. These measurements have
the same single-run timing limitations. Dataset reports are saved as
`benchmark-results/review-final-{smoke,large,fleet,mixed}.json`.
