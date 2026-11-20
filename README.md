# photo_autotag

Automatically tag photos and videos with EXIF keywords taken from the folders they're in.

If your archive is already organised into meaningful folders, this script copies that organisation into the files themselves, so photo apps (Lightroom, digiKam, Synology Photos, Apple Photos import, etc.) can search and filter by it. Dates, camera filenames and other noise are stripped, so you get keywords you'd actually want to search for:

| File path | Keywords written |
|---|---|
| `2024/2024-07/2024-07-12~20 Japan Trip/IMG_1234 Fushimi Inari.HEIC` | `Japan Trip`, `Fushimi Inari` |
| `Family/2023 Christmas/DSC06087.JPG` | `Family`, `Christmas` |
| `2019/2019-03/2019-03-22~24 First Camping Trip/IMG_3819.jpg` | `First Camping Trip` |

It's incremental: each run only processes files that are new or changed since the last run, so you can schedule it to tag new photos as they arrive.

## Features

- **Folder names become keywords**, with leading dates and date ranges removed (`2023-05 Trip`, `2006-11~2007-02 Trip`, `2023年5月 旅行`, `2015-08~09 Atlanta`).
- **Human-named filenames become keywords too** (`IMG_1234 Birthday cake.jpg` → `Birthday cake`). Camera, phone, scanner and app-generated names are ignored (`IMG_1234`, `_MG_1552`, `DSC06087`, `PXL_20230101_…`, `FullSizeRender`, `微信图片_…`, timestamps, hashes, random download IDs).
- **Keeps your own keywords.** Tags you added by hand in another app are preserved; the script remembers which keywords it wrote, and only replaces those.
- **Follows folder renames.** Rename `Trip` to `Japan Trip` and the next run swaps the keyword on every file in it.
- **Backfills missing dates.** Files with no capture date get one from a `YYYY-MM-DD` in the path, or failing that, the file's modification time.
- **Fast incremental runs.** A local SQLite cache records each file's size and modification time, so unchanged files are skipped without being opened.
- **Safe to schedule.** A lock file prevents overlapping runs, failed files aren't retried on every run, and every change is written to an audit log.
- **CJK-aware.** Handles Chinese/Japanese folder names, full-width punctuation and `年/月/日` dates.

## Requirements

- **Python 3.8+.** Standard library only; nothing to `pip install`.
- **[ExifTool](https://exiftool.org/)** by Phil Harvey, available on your `PATH`:

  | Platform | Install |
  |---|---|
  | macOS | `brew install exiftool` |
  | Debian / Ubuntu | `sudo apt install libimage-exiftool-perl` |
  | Windows | Download from exiftool.org and rename `exiftool(-k).exe` to `exiftool.exe` |
  | Synology / other NAS | Download the Unix tarball from exiftool.org and unpack it (needs Perl), or install a community package |

  Check it works with `exiftool -ver`.

- Write access to the photo files. The script modifies files in place.

## Installation

Copy `photo_autotag.py` into the **root folder of your photo archive**. By default the script treats its own folder as the archive root, and keeps its database and log there.

```
Photos/
├── photo_autotag.py
├── 2023/
│   └── 2023-12/
│       └── 2023-12-25 Christmas/
└── Family/
```

To keep the script somewhere else, use `--root /path/to/Photos`.

## Quick start

> **Back up first.** The script rewrites metadata in place (`-overwrite_original`), so no `_original` copies are kept. Take a snapshot or backup before the first real run.

```bash
# 1. Preview the keywords your folder names will produce. Reads paths only; very fast.
python3 photo_autotag.py --keywords-report preview.csv

# 2. Dry run: reads each file's metadata and logs what would change, without writing.
python3 photo_autotag.py --dry-run

# 3. Tag everything for real.
python3 photo_autotag.py

# 4. Later, after adding photos: only new or changed files are processed.
python3 photo_autotag.py
```

Open `preview.csv` in a spreadsheet: it lists every keyword with how many files get it and an example path. That's the quickest way to spot folder names that shouldn't become keywords (see [Configuration](#configuration)).

The dry run doesn't update the cache, so the real run still checks every file.

## Command-line options

| Option | Description |
|---|---|
| *(none)* | Incremental run: tag new or changed files, skip everything else. |
| `--dry-run` | Log what would change without writing to any file. Combine with `--force` to check the whole archive. With `--prune`, lists stale rows without deleting them. |
| `--force` | Ignore the cache and re-check every file. Use after changing the keyword rules or configuration. Slow on large archives. |
| `--root PATH` | Archive root. Default: the folder the script is in. |
| `--db PATH` | Cache database. Default: `<root>/photo_autotag.db`. |
| `--log PATH` | Audit log. Default: `<root>/photo_autotag.log`. |
| `--keywords-report CSV` | Write a CSV of every keyword the rules derive from your paths, with a file count and an example path, then exit. Never reads or writes photo files. |
| `--prune` | Remove cache rows for files that no longer exist (deleted, moved or renamed), then exit. Refuses to remove more than 5% of rows, because that usually means the wrong root or an unmounted share. |
| `--yes` | Allow `--prune` to remove more than 5% of rows. |

## How keywords are derived

Each folder in the file's path, and the filename itself, is cleaned in turn:

1. **Dates are removed:** full dates anywhere (`2023-05-01`, `2023-05-01~05`, `2017-02-26-27`), leading years and year-months (`2023 `, `2023-05 `, `2023.05 `, `2023年5月`), year-month ranges (`2014-12~2015-01`), and leading month-day (`11-26 `).
2. **Camera tokens are removed:** a leading code with at least 3 digits and at most 6 letters (`IMG_1234`, `DSC06087`, `B20171112_3`, `PC1234`). Short names like `V2`, `2nd Day` or `Room 4` are kept.
3. **Filename-only clean-up:** edit suffixes (`-edited`, ` copy 2`, `-ANIMATION`, `.TRIM`), counters (`_3`, ` (2)`, ` 0001`) and trailing camera tokens (`Beach IMG_5795` → `Beach`) are removed.
4. **The result is dropped entirely** if it has no letters left, is a bare year or a single letter, is a generic word (`photo`, `video`, `untitled`, `照片`…), looks like a hash or random ID, or is an ignored folder name (`iPhone`, `DCIM`, `images`…).

Keywords are de-duplicated, and kept in path order (outermost folder first).

**Existing keywords.** On each file, the final keyword list is:

- the keywords derived from the path, **plus**
- any keyword already on the file that the script didn't write itself, as long as it doesn't look like noise or like a raw folder name.

The cache records which keywords the script wrote to each file, so a keyword you added by hand survives, while one the script wrote earlier is replaced when the folder is renamed. A moved or renamed file is recognised by its size and modification time, which a move doesn't change.

On a fresh install, existing keywords that are clearly path-derived are also cleaned up: a raw folder name (`2023-05 Japan Trip`), an ignored folder name, or a folder keyword with an extra date or number (`2 School Trip`). To make path-derived keywords the *only* keywords, set `keep_foreign_keywords` to `false`.

**Date backfill.** If a file has no `DateTimeOriginal`, `CreateDate` or `QuickTime:CreateDate`, the script writes all three, using the first `YYYY-MM-DD` found in the path (at midnight) or, if there isn't one, the file's modification time. Files that already have a date are never changed.

## Supported files

Only these extensions are processed; everything else (sidecar `.json` files, audio, documents, etc.) is skipped:

`.jpg .jpeg .jpe .png .gif .heic .heif .webp .avif .tif .tiff .psd .dng .cr2 .cr3 .arw .nef .raf .orf .rw2 .mov .mp4 .m4v .3gp`

Hidden files and folders, and folders named in `excluded_dirs` (by default Synology's `#snapshot`, `#recycle` and `@eaDir` thumbnail folders), are skipped.

Keywords are written with ExifTool's `-Keywords` tag. For JPEG and TIFF that's IPTC Keywords, which has a 64-byte limit per keyword (about 21 CJK characters), so longer keywords are truncated cleanly before writing.

## Configuration

The defaults suit most archives. To adjust them for yours, create `photo_autotag.json` next to the script (copy `photo_autotag.example.json`). Every key is optional, and the lists are **added** to the built-in defaults:

```json
{
  "excluded_dirs": ["Screenshots to sort"],
  "folder_ignore": ["Exports", "Selects"],
  "generic_words": ["wallpaper"],
  "keep_foreign_keywords": true,
  "use_stem_keywords": true
}
```

| Key | Built-in default | Purpose |
|---|---|---|
| `excluded_dirs` | `#snapshot`, `#recycle`, `@eaDir` | Folder names never scanned (exact name). |
| `folder_ignore` | `iPhone`, `Android`, `DCIM`, `Camera Roll`, `MobileBackup`, `CD`, `images`, `相冊`, `原片`, … | Folder names that should never become keywords (whole name, case-insensitive). Add your own structural folders here. |
| `generic_words` | `photo`, `video`, `untitled`, `unnamed`, app names like `FullSizeRender`, … | Words that are never useful as keywords on their own (case-insensitive). |
| `keep_foreign_keywords` | `true` | Preserve keywords the script didn't write. |
| `use_stem_keywords` | `true` | Derive a keyword from human-named filenames. |

Keeping your settings in this file means you can update `photo_autotag.py` without merging changes. The list of supported file types (`MEDIA_EXTS`) is the only setting that has to be changed in the script itself.

After changing any setting, check the effect with `--keywords-report`, then apply it to already-tagged files with `--force --dry-run` followed by `--force`.

## Running on a schedule

Each run only touches new or changed files and exits quickly when there's nothing to do, so it's suitable for a nightly job. If a run is still going when the next one starts, the second exits immediately.

**cron (Linux / macOS):**
```cron
0 3 * * * /usr/bin/python3 /path/to/Photos/photo_autotag.py >> /path/to/Photos/cron.out 2>&1
```

**Synology Task Scheduler** (Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script):
```bash
export PATH="$PATH:/usr/local/bin:/opt/bin"   # wherever exiftool is installed
python3 /volume1/<share>/Photos/photo_autotag.py
```
Scheduled tasks start with a minimal `PATH`, so make sure the folder containing `exiftool` is on it (find it over SSH with `which exiftool`).

## Files the script creates

| File | Contents |
|---|---|
| `photo_autotag.db` | SQLite cache: one row per file with its size, modification time, original keywords, the keywords the script wrote, date, and status (`SUCCESS`, `SKIPPED` or `ERROR`). Deleting it is safe: the next run re-checks every file and rebuilds it, but the script then can't tell which existing keywords it wrote, so after a folder rename both old and new keywords may remain until you clean them up. |
| `photo_autotag.log` | Append-only audit log: every file changed, its new keywords, the keywords removed, any date backfilled, and a summary per run. |
| `.photo_autotag.lock` | Lock file that prevents overlapping runs. |

Your `photo_autotag.json`, if you create one, sits alongside these.

**These files contain your folder and file names. Don't commit them to a public repository.** The included `.gitignore` excludes them, including `photo_autotag.json`.

## Troubleshooting

**`exiftool: command not found`, or every file fails.** ExifTool isn't on the `PATH` of the shell or scheduled task running the script. Run `exiftool -ver` in the same environment.

**`Not a valid HEIC (looks more like a JPEG)`, or a similar extension mismatch.** The file's extension doesn't match its contents, and ExifTool refuses to write to it. Rename such files to their real extension (test first, then replace `-testname` with `-filename`):
```bash
exiftool -r -ext heic -if '$FileType ne "HEIC"' '-testname<%f.$FileTypeExtension' .
exiftool -r -ext png  -if '$FileType ne "PNG"'  '-testname<%f.$FileTypeExtension' .
exiftool -r -ext jpg -ext jpeg -if '$FileType ne "JPEG"' '-testname<%f.$FileTypeExtension' .
```
ExifTool won't overwrite an existing file of the same name.

**Errors like `Error reading OtherImageStart data` or `Bad APP13 resource`.** The file's existing metadata is damaged. Minor problems are already ignored (`-m`); files with serious damage are logged, cached as `ERROR`, and skipped until they change. `--force` retries them.

**`REFUSING to prune …`.** More than 5% of cached files look missing. Check that `--root` is right and the share is mounted, inspect the list with `--prune --dry-run`, and only then use `--yes`.

**A folder name became a keyword you don't want.** Add it to `folder_ignore` in `photo_autotag.json` (or `generic_words` if it's a filename word), then run with `--force --dry-run`, then `--force`.

## Limitations

- Rules are tuned for date-prefixed, event-named folders (`YYYY-MM-DD Event`). Other naming schemes work, but run `--keywords-report` first to check the output.
- Writes go through one `exiftool` process per changed file, so a first run on tens of thousands of files can take hours. Later runs are fast.
- Formats ExifTool can't write (e.g. AVI, MPG, WAV) are skipped.

## Licence

[MIT](LICENSE). ExifTool is a separate project, distributed under the same terms as Perl, and is not included in this repository.
