# showparse

`showparse` is a prompt-delimited CLI parser for network collection `.dat` files.
It extracts real command output blocks first, then filters them with query-aware
grep and block-context logic so results stay accurate and useful for notes and
reporting.

## Core Usage

Standard query mode:

```bash
python3 showparse.py -q "show version" test1/data/*.dat
python3 showparse.py --no-color -q "show version" test1/data/*.dat
python3 showparse.py --no-banner -q "show version" test1/data/*.dat
python3 showparse.py -q "show run:hostname|username|logging host" test1/data/*.dat
python3 showparse.py -q "~:show run:username" test1/data/*.dat
python3 showparse.py -q "%:show logging:Trap logging: level \\w+" test1/data/*.dat
python3 showparse.py -q "#:show version" test1/data/*.dat
```

Block query mode:

```bash
python3 showparse.py -Q "show run:shutdown" test1/data/*.dat
python3 showparse.py -Q "~@:show run:switchport mode access|shutdown" test1/data/*.dat
python3 showparse.py -Q "@:show run:switchport mode access|shutdown" test1/data/*.dat
python3 showparse.py -Q "/@:show run:Shutdown|switchport mode access" test1/data/*.dat
python3 showparse.py -Q "#@%:show run:switchport mode access|shutdown" test1/data/*.dat
```

Notes mode:

```bash
python3 showparse.py -q "show version" -n test1/data/*.dat
python3 showparse.py -q "show version" -n -o /tmp/showparse-notes.txt test1/data/*.dat
```

During the notes-mode compile/dedupe phase, showparse writes a live
`(completed/total)` progress counter to `stderr`.

## Current CLI Surface

- `-q` standard query mode
- `-Q` block-aware query mode
- `-A` require all queries to return content for a file
- `-r` raw grep-style output
- `--no-color` print the per-file filename banner without ANSI color
- `--no-banner` suppress the per-file filename banner in normal mode
- `-n` notes mode with deduplication
- `-o` save the final notes report to a file

## Query Modifiers

- `%` matched substring only
- `~` show matched command first
- `#` count non-empty rendered lines after all other modifiers finish
- `+` all matching prompt-delimited commands for that query
- `@` parent + matched child lines only (`-Q` only)
- `/` case-sensitive pattern matching

Note: keep queries containing `#` quoted so your shell does not treat `#` as a comment character.

## Testing

Run the regression suite:

```bash
python3 -m unittest discover -s tests -v
```

See also:

- `SHOWPARSE_CODE_GUIDE.txt` for the canonical future-session handoff and code guide
- `REGRESSION_CHECKLIST.md` for smoke-test commands

## showparse_fsm Companion

`showparse_fsm.py` is the structured-data companion to `showparse.py`.
It keeps the same prompt-aware bulk extraction idea, but applies one TextFSM
template per run and emits file-rooted JSON instead of human-oriented text.

Install the runtime dependency:

```bash
python3 -m pip install textfsm
```

Example:

```bash
python3 showparse_fsm.py -q "show version" --template /path/to/show_version.textfsm test1/data/*.dat
```

The output root is always a JSON array with one object per processed file, plus
nested `matches` entries when `+` is used.

If you want help learning how to script against that JSON, see:

- `SHOWPARSE_FSM_JSON_HELPER_GUIDE.txt`
- `showparse_fsm_json_helper_template.py`

## Large Practice Fixtures

The fast regression fixtures live in `test1/data`. If you want realistic
large-file parsing practice without touching that baseline, generate the
separate large fixtures in `test1/data_large`:

```bash
python3 generate_large_fixtures.py
```

By default this creates large Cisco-style practice captures at about
`4,000,000` bytes each while preserving the original sample fixture content in
the middle of each file.
