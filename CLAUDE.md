# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

`photo_autotag.py` writes EXIF keywords to photos and videos, derived from their folder names (and from human-named filenames), and backfills missing capture dates. It runs incrementally against a real, irreplaceable photo archive, so **correctness and safety matter more than features**.

- One file, Python 3.8+, **standard library only**. Don't add dependencies.
- All metadata reads and writes go through the external `exiftool` binary.
- Sections in the script: SETTINGS → KEYWORD RULES → FILES, CACHE AND EXIFTOOL → COMMANDS.
- User docs live in `README.md`. Update it whenever options, defaults or behaviour change.

## Invariants (don't break these)

- **Unicode:** relative paths and keywords are NFC-normalised (`iter_media_files`, and keywords read from files). macOS SMB clients can return NFD names; skipping this causes every CJK or accented file to be rewritten and `--prune` to misfire.
- **One keyword per exiftool argument** (`-Keywords+=<kw>`). Never join keywords with commas or use `-sep`: folder names can contain commas.
- **Cache semantics:** `file_hash` is a `size:<bytes>` fingerprint and `mod_time` is the mtime, both taken *after* a successful write. `new_keywords` is a JSON list of exactly what the script wrote. It is what lets hand-added keywords survive (`build_final_keywords`) and lets renamed or moved files be matched by size and mtime (`previously_written`).
- **Keyword comparison is exact set equality** with what's on the file, not a subset check.
- **IPTC 64-byte limit:** keywords for `IPTC_EXTS` are truncated on a character boundary (`fit_keyword`) before comparing and writing, so re-runs converge.
- **`--prune` safety:** it compares against a fresh NFC scan and refuses to delete more than 5% of rows without `--yes`. Keep both.
- **State lives next to the script** (`STATE_DIR`): config, database, log and lock file. The archive (`--root`) only has its photos modified.
- **Failed writes are cached as `ERROR`** so they aren't retried every run. `--force` retries them.

## Changing keyword rules

Rules are regexes plus small helpers (`strip_dates`, `strip_machine_token`, `is_noise_keyword`, `clean_folder_keyword`, `clean_stem_keyword`). They are easy to regress.

1. Generate `--keywords-report` on a representative path list before and after the change.
2. Diff the two reports and review every changed keyword, not just the examples you meant to fix.
3. Check that real names survive: short tokens (`V2`, `2nd Day`, `Room 4`), underscore names (`Japan_Trip_2019`), CJK text, and names with commas.
4. Archive-specific folder or word lists belong in the user's `photo_autotag.json`, not in the built-in defaults. Only add defaults that are generic (e.g. `DCIM`, `@eaDir`).

`compute_expected_keywords(Path(...))` can be called directly from a Python shell for quick checks; it never touches files.

## Testing

There's no test suite yet. Before proposing a change:

- `python3 -m py_compile photo_autotag.py`
- For logic that writes files, test in a temporary directory with a **fake `exiftool`** first on `PATH`. A small Python script that answers `-stay_open` reads with JSON (`[{"SourceFile": ..., "Keywords": [...]}]` followed by `{ready}`), records `-Keywords+=` arguments to a JSON file, and touches the file's mtime is enough to exercise a full run: first run updates, second run reports everything unchanged, and a renamed folder swaps the keyword.
- Never run against a real archive without `--dry-run` first. Recommend a backup or snapshot before any `--force` run.

## Privacy

This repo is public. Never commit `*.db`, `*.log`, `*.csv` or `photo_autotag.json`: they contain the user's folder and file names. `.gitignore` covers them, but check `git status` anyway. Keep code comments and docs free of personal examples (real names, addresses, the user's folder names); use neutral ones like `Japan Trip` or `School concert IMG_5795`.

## Style

- Explain *why* in comments where a rule exists because of a real-world case; keep the examples neutral.
- Prefer small, explicit regexes with a comment showing what they match over clever ones.
- Keep CLI output and log lines concise and greppable (`SUCCESS:`, `NOTICE on`, `ERROR on`, `DRY RUN:`).
