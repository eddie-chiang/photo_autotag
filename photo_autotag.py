#!/usr/bin/env python3
"""
photo_autotag.py -- incremental EXIF keyword tagging for a folder-organised
photo archive.

Keywords come from folder names (and human-named filenames), with dates,
camera filenames and other noise stripped:

    2024/2024-07/2024-07-12~20 Japan Trip/IMG_1234 Fushimi Inari.HEIC
      -> Keywords: "Japan Trip", "Fushimi Inari"

Each run only looks at files that are new or changed since the last run, so
it can be scheduled to tag new photos automatically. See README.md.
"""
import argparse
import csv
import functools
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

# =============================================================================
# SETTINGS
# Defaults live here. Archive-specific additions belong in photo_autotag.json
# next to the script (see README), so this file can be updated without merging.
# =============================================================================
# The script's own folder holds its settings, cache, log and lock file. It is
# also the default archive root, so the simplest setup is to drop the script into
# the archive. To run from another machine, keep it in a local folder and point
# --root at the mounted share.
STATE_DIR = Path(__file__).resolve().parent
SOURCE_DIR = STATE_DIR  # archive root; override with --root
LOG_FILE = "photo_autotag.log"
DB_FILE = "photo_autotag.db"
LOCK_FILE = ".photo_autotag.lock"
CONFIG_FILE = "photo_autotag.json"

# Folder names never scanned (Synology snapshots, recycle bin, thumbnails).
EXCLUDED_DIRS = {"#snapshot", "#recycle", "@eaDir"}

# Only these file types are tagged.
MEDIA_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".heic", ".heif", ".webp", ".avif",
    ".tif", ".tiff", ".psd", ".dng", ".cr2", ".cr3", ".arw", ".nef", ".raf",
    ".orf", ".rw2", ".mov", ".mp4", ".m4v", ".3gp",
}

# Keep keywords on a file that this script didn't write (hand-applied tags,
# tags from other apps). False makes path-derived keywords the only keywords.
KEEP_FOREIGN_KEYWORDS = True

# Derive a keyword from human-named filenames ("IMG_1234 Birthday cake" ->
# "Birthday cake"). False uses folder names only.
USE_STEM_KEYWORDS = True

# Folder names that should never become keywords: structural folders from
# phone backups, CDs and photographer deliveries. Whole name, case-insensitive.
FOLDER_IGNORE = {
    "mobilebackup", "iphone", "android", "dcim", "camera", "camera roll",
    "cd", "cdxdata", "images", "相冊", "相册", "原片",
}

# Words that are never useful keywords on their own. Compared after removing
# digits, spaces and separators, case-insensitive.
GENERIC_WORDS = {
    "movie", "video", "image", "img", "photo", "photos", "picture", "pic",
    "scan", "copy", "untitled", "unnamed", "edited", "edit", "tweaked", "export",
    "thumbnail", "jpeg", "jpg", "fullsizerender", "imagefromios", "filefromios",
    "screenrecording", "mmexport", "lpimage", "gptempdownload",
    "照片", "图片", "圖片", "相片", "微信图片", "微信圖片", "副本",
}

# IPTC:Keywords allows 64 bytes per entry; longer keywords are truncated cleanly
# up front so re-runs see the same value exiftool stored.
IPTC_KEYWORD_BYTES = 64
IPTC_EXTS = {".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".psd"}


def load_config(state_dir: Path):
    """Merge optional photo_autotag.json (next to the script) into the defaults:
    {"excluded_dirs": [...], "folder_ignore": [...], "generic_words": [...],
     "keep_foreign_keywords": true, "use_stem_keywords": true}"""
    global KEEP_FOREIGN_KEYWORDS, USE_STEM_KEYWORDS
    path = state_dir / CONFIG_FILE
    if not path.is_file():
        return
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    EXCLUDED_DIRS.update(cfg.get("excluded_dirs", []))
    FOLDER_IGNORE.update(w.casefold() for w in cfg.get("folder_ignore", []))
    GENERIC_WORDS.update(w.casefold() for w in cfg.get("generic_words", []))
    KEEP_FOREIGN_KEYWORDS = cfg.get("keep_foreign_keywords", KEEP_FOREIGN_KEYWORDS)
    USE_STEM_KEYWORDS = cfg.get("use_stem_keywords", USE_STEM_KEYWORDS)


# =============================================================================
# KEYWORD RULES
# =============================================================================
_YEAR = r"(?:19|20)\d{2}"
_MONTH = r"(?:0?[1-9]|1[0-2])"
_DAY = r"(?:0?[1-9]|[12]\d|3[01])"
_DSEP = r"[-_./]"
_DATE = (
    rf"(?:{_YEAR}年(?:{_MONTH}月(?:{_DAY}[日号號])?)?"
    rf"|{_YEAR}(?:{_DSEP}{_MONTH}(?:{_DSEP}{_DAY})?)?)"
)
# End of a range: "2007-02", "09", "12-05", "2019", "13" (day only)
_RANGE_END = rf"(?:{_YEAR}(?:{_DSEP}{_MONTH}(?:{_DSEP}{_DAY})?)?|{_MONTH}(?:{_DSEP}{_DAY})?|{_DAY})"
_RANGE_SEP = r"\s*[~～–—]\s*"
_TRAIL_SEP = r"[\s_\-–—.,:：、]"
_TIDY_CHARS = " \t-_~～.,:：、–—+"

# Leading date or date range: "2023 ", "2025-11 ", "2006-11~2007-02 ", "2015-08~09 ",
# "2023年5月 ". Must be followed by a separator, non-ASCII text, or the end.
DATE_PREFIX = re.compile(
    rf"^\s*{_DATE}(?:{_RANGE_SEP}{_RANGE_END})?(?={_TRAIL_SEP}|$|[^\x00-\x7F]){_TRAIL_SEP}*"
)
# Full YYYY-MM-DD anywhere, optionally a range: "2023-05-01~05", "2017-02-26-27"
FULL_DATE_ANYWHERE = re.compile(
    rf"{_YEAR}-\d{{2}}-\d{{2}}(?:(?:{_RANGE_SEP}|-){_RANGE_END})?(?!\d)"
)
# YYYY-MM anywhere: "Grandparents visit 2013-12 + 2014-02"
YEAR_MONTH_ANYWHERE = re.compile(
    rf"(?<!\d){_YEAR}-{_MONTH}(?:{_RANGE_SEP}{_RANGE_END})?(?![\d-])"
)
MONTH_DAY_PREFIX = re.compile(r"^\d{1,2}-\d{1,2}\s+")
TRAILING_CJK_YEAR = re.compile(rf"(?<=[^\x00-\x7F]){_YEAR}$")  # "春節聚會2005"

# Camera / scanner / app code at the start: IMG_1234, _MG_1552, DSC06087,
# B20171112_3-95, PC1234, 20190101-ABC_1234, "12. DSC_1234". Counted as
# machine-generated only with >=3 digits and <=6 letters, so "V2", "2nd Day"
# and "Room 4" are kept.
MACHINE_TOKEN = re.compile(r"^(?:\d{1,3}\.\s*)?([A-Za-z_]*\d[0-9A-Za-z_\-.()]*)")

# Filename edit/export suffixes: "-ANIMATION", "-EFFECTS-edited", " copy 2", ".TRIM"
SUFFIX_NOISE = re.compile(
    r"(?:[\s_.\-]*(?:copy|edited|edit|tweaked|effects|animation|collage|trim|pano|resized"
    r"|recovered|raw|livephoto|color_pop|normal|watermarked|副本)(?:\s*\d+)?)+$",
    re.IGNORECASE,
)
# Filename tails: counters ("_3", " (2)", " 0001"), timestamps ("_20200101-123456")
# and trailing upper-case camera codes ("School concert IMG_5795").
NUMERIC_TAIL = re.compile(
    r"(?:[_\-.]\d{1,4}|\s*\(\d+\)|\s+\d{3,}|[\s_\-.]*\d{6,}(?:[_\-.]\d+)*"
    r"|[\s_\-.]*(?<![A-Za-z])[A-Z]{1,5}_?\d{3,})+$"
)

GUID = re.compile(
    r"^(?:trim\.)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:_[0-9a-f]+)?$",
    re.IGNORECASE,
)
GOPRO = re.compile(r"^(?:GOPR|GP|GX)\d{4,6}$", re.IGNORECASE)
LONG_HEX = re.compile(r"^(?=.*\d)[0-9a-f._\-]{16,}$", re.IGNORECASE)
# Random download/share IDs: ASCII, no spaces, 16+ chars, mixed case + digits,
# switching character class often ("6BoS5ZkkRcWJ-d703LUd1"), unlike real names
# such as "Japan_Trip_2019" (checked in _is_random_id).
RANDOM_ID = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)[@A-Za-z0-9_\-]{16,}$")
# OS-generated names: "Movie on 3-01-16 at 1.12 PM", "ScreenRecording_11-17-2026 ..."
SYSTEM_NAME = re.compile(
    r"^(?:Movie on |Screen ?Recording[\s_]|Screenshot[\s_]|Screen Shot )", re.IGNORECASE
)


def _tidy(s: str) -> str:
    return re.sub(r"\s{2,}", " ", s).strip(_TIDY_CHARS)


def _is_random_id(s: str) -> bool:
    if not RANDOM_ID.match(s):
        return False
    chars = [c for c in s if c.isalnum()]
    kind = lambda c: "d" if c.isdigit() else ("u" if c.isupper() else "l")  # noqa: E731
    switches = sum(kind(a) != kind(b) for a, b in zip(chars, chars[1:]))
    return switches >= len(chars) / 3


def _looks_like_id(s: str) -> bool:
    """Hashes, GUIDs, OS-generated names and long digit-run IDs."""
    return bool(
        GUID.match(s) or GOPRO.match(s) or LONG_HEX.match(s) or SYSTEM_NAME.match(s)
        or (" " not in s and re.search(r"\d{9,}", s))
    )


def strip_dates(s: str) -> str:
    s = FULL_DATE_ANYWHERE.sub(" ", s)
    s = YEAR_MONTH_ANYWHERE.sub(" ", s)
    s = _tidy(s)
    for _ in range(3):  # "2018-2019 Trip" needs two passes
        new = DATE_PREFIX.sub("", s, count=1)
        if new == s:
            break
        s = new
    s = MONTH_DAY_PREFIX.sub("", s)
    s = TRAILING_CJK_YEAR.sub("", _tidy(s))
    return _tidy(s)


def strip_machine_token(s: str) -> str:
    for _ in range(3):  # "IMG_2497 IMG_2471 Beach day" has two codes
        m = MACHINE_TOKEN.match(s)
        if not m:
            break
        tok = m.group(1)
        if sum(c.isdigit() for c in tok) < 3 or sum(c.isalpha() for c in tok) > 6:
            break
        s = _tidy(s[m.end():])
    return s


def is_noise_keyword(token: str) -> bool:
    token = token.strip()
    if not token or not any(c.isalpha() for c in token):  # "2018-02", "1042"
        return True
    if len(token) == 1 and token.isascii():  # "a.jpg"
        return True
    if _looks_like_id(token) or _is_random_id(token):
        return True
    if " " not in token and len(token) > 40:  # base64-style download names
        return True
    if strip_machine_token(token) == "":  # "_MG_1552", "DSC06087", "B20171112_3"
        return True
    return re.sub(r"[\d\s_\-.()#]+", "", token).casefold() in GENERIC_WORDS


def clean_folder_keyword(part: str) -> str:
    s = strip_machine_token(strip_dates(part))  # "B20151031_1 Wedding proofs" -> "Wedding proofs"
    if s.casefold() in FOLDER_IGNORE or is_noise_keyword(s):
        return ""
    return s


def clean_stem_keyword(stem: str) -> str:
    if _looks_like_id(stem):  # check whole names before stripping can leave a fragment
        return ""
    s = re.sub(r"\.(?:jpe?g|png|heic|gif|tiff?|mov|mp4|m4v)$", "", stem, flags=re.I)  # "x.JPG.jpg"
    s = re.sub(r"\s*\(\d+\)$", "", s)  # "-ANIMATION(5)" -> "-ANIMATION"
    s = SUFFIX_NOISE.sub("", s)
    s = strip_machine_token(_tidy(s))  # before tail-stripping, so "AL2I1234" goes whole
    s = strip_dates(s)
    s = NUMERIC_TAIL.sub("", s)
    s = _tidy(re.sub(r"^\d+\s+", "", s))
    return "" if is_noise_keyword(s) else s


def _dedupe(items):
    seen = set()
    return [k for k in items if k and not (k in seen or seen.add(k))]


def compute_expected_keywords(rel_path: Path):
    keywords = [clean_folder_keyword(p) for p in rel_path.parent.parts]
    if USE_STEM_KEYWORDS:
        keywords.append(clean_stem_keyword(rel_path.stem))
    return _dedupe(keywords)


def fit_keyword(kw: str, byte_limit=None) -> str:
    """Truncate to the IPTC byte limit on a character boundary."""
    if not byte_limit:
        return kw
    b = kw.encode("utf-8")
    return kw if len(b) <= byte_limit else b[:byte_limit].decode("utf-8", "ignore").rstrip()


def _is_path_derived(kw: str, rel_path: Path, expected) -> bool:
    """True if kw clearly came from this file's path rather than from a person:
    - a raw folder name or filename, or one in FOLDER_IGNORE;
    - an expected keyword plus only digits/dates/separators, e.g. "2 School Trip"
      or "2021-02 School Trip" left over after the folder was renamed;
    - a shortened form of an expected keyword ("Celebration" vs "Celebration Night")."""
    raw = set(rel_path.parent.parts) | {rel_path.stem}
    if kw in raw or kw.casefold() in FOLDER_IGNORE:
        return True
    for e in expected:
        if not e or e == kw:
            continue
        if e in kw and not any(c.isalpha() for c in kw.replace(e, " ")):
            return True
        if e.startswith(kw + " "):
            return True
    return False


def build_final_keywords(rel_path, current, previously_written=(), byte_limit=None):
    """Path-derived keywords, plus keywords already on the file that weren't
    written by this script (per the cache) and don't look path-derived or noisy."""
    expected = compute_expected_keywords(rel_path)
    final = [fit_keyword(k, byte_limit) for k in expected]
    if KEEP_FOREIGN_KEYWORDS:
        written = set(previously_written)
        for k in current:
            if k in written or is_noise_keyword(k) or _is_path_derived(k, rel_path, expected):
                continue
            final.append(fit_keyword(k, byte_limit))
    return _dedupe(final)


# =============================================================================
# FILES, CACHE AND EXIFTOOL
# =============================================================================
def retry_on_disk_io(max_retries=5, base_delay=2, max_delay=900):
    """Retry transient I/O errors (e.g. a NAS share dropping) with backoff.
    Missing files and permission errors are raised immediately."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (FileNotFoundError, PermissionError):
                    raise
                except OSError as e:
                    if attempt == max_retries:
                        raise
                    delay = min(max_delay, base_delay * 2 ** (attempt - 1)) + random.uniform(0, 1)
                    print(f"[{func.__name__}] I/O error ({e}); retry {attempt}/{max_retries} in {delay:.1f}s")
                    time.sleep(delay)
        return wrapper
    return decorator


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_files (
            file_path TEXT PRIMARY KEY,
            file_hash TEXT,           -- change fingerprint: "size:<bytes>"
            mod_time REAL,
            original_file_date TEXT,
            original_keywords TEXT,
            new_date TEXT,
            new_keywords TEXT,        -- JSON list of keywords this script wrote
            status TEXT,              -- SUCCESS | SKIPPED | ERROR
            processed_date TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fingerprint ON processed_files (file_hash, mod_time)")
    conn.commit()
    return conn


def save_row(conn, rel_path_str, fingerprint, mtime, orig_date, orig_keywords,
             new_date, new_keywords, status):
    conn.execute(
        "INSERT OR REPLACE INTO processed_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rel_path_str, fingerprint, mtime, orig_date, json.dumps(orig_keywords, ensure_ascii=False),
         new_date, json.dumps(new_keywords, ensure_ascii=False), status,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()


def _parse_keywords(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(k) for k in parsed]
    except (ValueError, TypeError):
        pass
    return [k.strip() for k in str(value).split(", ")]  # rows from older versions


def previously_written(conn, rel_path_str, fingerprint, mtime):
    """Keywords this script last wrote to the file. Falls back to a unique
    size+mtime match, so files in a renamed or moved folder are recognised."""
    row = conn.execute(
        "SELECT new_keywords FROM processed_files WHERE file_path = ?", (rel_path_str,)
    ).fetchone()
    if not row:
        rows = conn.execute(
            "SELECT new_keywords FROM processed_files WHERE file_hash = ? AND mod_time = ? LIMIT 2",
            (fingerprint, mtime),
        ).fetchall()
        row = rows[0] if len(rows) == 1 else None
    return _parse_keywords(row[0]) if row else []


@retry_on_disk_io()
def get_fingerprint(file_path):
    """Size + mtime. Any edit to a photo, including exiftool's own writes,
    changes the mtime, so the file never needs to be read to detect changes."""
    st = file_path.stat()
    return f"size:{st.st_size}", st.st_mtime


@retry_on_disk_io()
def get_file_mtime_str(file_path):
    return datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%Y:%m:%d %H:%M:%S")


def extract_date_from_path(path_str):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", path_str)
    return f"{m.group(1)}:{m.group(2)}:{m.group(3)} 00:00:00" if m else None


def iter_media_files():
    for root, dirs, files in os.walk(SOURCE_DIR):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or Path(name).suffix.lower() not in MEDIA_EXTS:
                continue
            file_path = Path(root) / name
            # NFC-normalise so paths and keywords are identical whether the share is
            # read on the NAS or over SMB from a Mac (which can return NFD names).
            rel = unicodedata.normalize("NFC", str(file_path.relative_to(SOURCE_DIR)))
            yield file_path, Path(rel)


class ExifToolSession:
    """One long-running exiftool process for fast metadata reads."""

    def __init__(self):
        self.proc = subprocess.Popen(
            ["exiftool", "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8",
        )

    def get_metadata(self, file_path):
        args = [str(file_path), "-j", "-Keywords", "-Subject", "-DateTimeOriginal",
                "-CreateDate", "-QuickTime:CreateDate", "-execute"]
        self.proc.stdin.write("\n".join(args) + "\n")
        self.proc.stdin.flush()
        lines = []
        while True:
            line = self.proc.stdout.readline()
            if not line or "{ready}" in line:
                break
            lines.append(line)
        try:
            data = json.loads("".join(lines))
            return data[0] if data else {}
        except ValueError:
            return {}

    def close(self):
        try:
            self.proc.stdin.write("-stay_open\nFalse\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


@retry_on_disk_io()
def run_exiftool(args):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace")


# =============================================================================
# COMMANDS
# =============================================================================
def process_photos(db_path, log_path, dry_run=False, force=False):
    mode = " [DRY RUN]" if dry_run else ""
    print(f"photo_autotag{mode}: scanning {SOURCE_DIR}")
    conn = init_db(db_path)
    et = ExifToolSession()
    updated = skipped = errors = 0

    def now():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"\n=== Run started {now()}{mode} (root {SOURCE_DIR}) ===\n")

            for file_path, rel_path in iter_media_files():
                rel = str(rel_path)
                try:
                    fingerprint, mtime = get_fingerprint(file_path)
                except OSError as e:
                    log.write(f"{now()} | ERROR reading {rel}: {e}\n")
                    errors += 1
                    continue

                if not force:
                    row = conn.execute(
                        "SELECT file_hash, mod_time FROM processed_files WHERE file_path = ?", (rel,)
                    ).fetchone()
                    if row and row[0] == fingerprint and row[1] == mtime:
                        skipped += 1
                        continue

                meta = et.get_metadata(file_path)
                current = meta.get("Keywords") or meta.get("Subject") or []
                if not isinstance(current, list):
                    current = [current]
                current = [unicodedata.normalize("NFC", str(k).strip()) for k in current if str(k).strip()]

                byte_limit = IPTC_KEYWORD_BYTES if file_path.suffix.lower() in IPTC_EXTS else None
                prior = previously_written(conn, rel, fingerprint, mtime)
                final = build_final_keywords(rel_path, current, prior, byte_limit)
                keywords_match = set(final) == set(current)

                orig_date = (meta.get("DateTimeOriginal") or meta.get("CreateDate")
                             or meta.get("QuickTime:CreateDate"))
                backfill, backfill_source = None, None
                if not orig_date:
                    backfill = extract_date_from_path(rel)
                    backfill_source = "path"
                    if not backfill:
                        try:
                            backfill, backfill_source = get_file_mtime_str(file_path), "file modified time"
                        except OSError:
                            pass
                new_date = orig_date or backfill

                if keywords_match and not backfill:
                    skipped += 1
                    if not dry_run:
                        save_row(conn, rel, fingerprint, mtime, orig_date, current, new_date, final, "SKIPPED")
                    continue

                removed = [k for k in current if k not in final]
                detail = (f"[FILE: {rel}] Keywords: [{', '.join(final) or 'None'}]"
                          f" | Removed: [{', '.join(removed) or 'None'}]"
                          f" | DateBackfill: [{f'{backfill} via {backfill_source}' if backfill else 'Not needed'}]")

                if dry_run:
                    print(f"[DRY RUN] would update: {rel}")
                    log.write(f"{now()} | DRY RUN: {detail}\n")
                    updated += 1
                    continue

                args = ["exiftool", "-m", "-overwrite_original", "-codedcharacterset=utf8", "-Keywords="]
                args += [f"-Keywords+={k}" for k in final]  # one per argument: commas stay intact
                if backfill:
                    args += [f"-DateTimeOriginal={backfill}", f"-CreateDate={backfill}",
                             f"-QuickTime:CreateDate={backfill}"]
                args.append(str(file_path))

                try:
                    res = run_exiftool(args)
                except OSError as e:
                    log.write(f"{now()} | ERROR on {rel}: {e}\n")
                    errors += 1
                    continue
                output = res.stdout.strip()

                if res.returncode == 0:
                    updated += 1
                    if "Warning:" in output:
                        log.write(f"{now()} | NOTICE on {rel}: {output} | {detail}\n")
                    else:
                        log.write(f"{now()} | SUCCESS: {detail}\n")
                    print(f"Updated: {rel}")
                    try:
                        fingerprint, mtime = get_fingerprint(file_path)  # cache post-write state
                    except OSError:
                        pass
                    save_row(conn, rel, fingerprint, mtime, orig_date, current, new_date, final, "SUCCESS")
                else:
                    errors += 1
                    log.write(f"{now()} | ERROR on {rel}: {output}\n")
                    print(f"ERROR on {rel}: {output}")
                    # Cached so normal runs skip it until the file changes; --force retries.
                    save_row(conn, rel, fingerprint, mtime, orig_date, current, new_date, final, "ERROR")
                log.flush()

            summary = (f"Run complete{mode}: {skipped} unchanged, "
                       f"{updated} {'would be updated' if dry_run else 'updated'}, {errors} errors")
            log.write(f"{now()} | {summary}\n")
            print(f"\n{summary}")
    finally:
        et.close()
        conn.close()


def keywords_report(out_csv):
    """Path-only preview: every keyword the rules derive, with a file count."""
    counts, example, total = Counter(), {}, 0
    for _, rel_path in iter_media_files():
        total += 1
        for k in compute_expected_keywords(rel_path):
            counts[k] += 1
            example.setdefault(k, str(rel_path))
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:  # -sig: Excel shows CJK
        w = csv.writer(f)
        w.writerow(["keyword", "file_count", "example_path"])
        for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            w.writerow([k, n, example[k]])
    print(f"{total} files, {len(counts)} distinct keywords -> {out_csv}")


def prune_db(db_path, dry_run=False, yes=False):
    """Delete cache rows for files that no longer exist."""
    conn = init_db(db_path)
    rows = [r[0] for r in conn.execute("SELECT file_path FROM processed_files")]
    print(f"Archive root: {SOURCE_DIR} (scanning...)")
    existing = {str(rel) for _, rel in iter_media_files()}  # NFC, same as the cache
    gone = [p for p in rows if p not in existing]
    for p in gone[:20]:
        print(f"  {'would remove' if dry_run else 'removed'}: {p}")
    if len(gone) > 20:
        print(f"  ... and {len(gone) - 20} more")
    if not dry_run and gone and not yes and len(gone) > max(20, 0.05 * len(rows)):
        print(f"REFUSING to remove {len(gone)} of {len(rows)} rows (>5%). Usually the archive "
              "root is wrong or the share isn't mounted. Check with --dry-run, then use --yes.")
    elif not dry_run and gone:
        conn.executemany("DELETE FROM processed_files WHERE file_path = ?", [(p,) for p in gone])
        conn.commit()
        print(f"{len(gone)} stale rows removed (of {len(rows)}).")
    else:
        print(f"{len(gone)} stale rows found (of {len(rows)}).")
    conn.close()


def acquire_lock():
    """Exit quietly if another run is in progress (POSIX only)."""
    try:
        import fcntl
    except ImportError:
        return None
    fh = open(STATE_DIR / LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another photo_autotag run is in progress; exiting.")
        sys.exit(0)
    return fh


def main():
    global SOURCE_DIR
    p = argparse.ArgumentParser(description="Incremental EXIF keyword tagging from folder names. See README.md.")
    p.add_argument("--dry-run", action="store_true", help="Log what would change without writing to any file")
    p.add_argument("--force", action="store_true", help="Ignore the cache and re-check every file (slow)")
    p.add_argument("--root", help="Archive root, e.g. a mounted share (default: the folder this script is in)")
    p.add_argument("--db", help=f"Cache database (default: {DB_FILE} next to the script)")
    p.add_argument("--log", help=f"Audit log (default: {LOG_FILE} next to the script)")
    p.add_argument("--keywords-report", metavar="CSV",
                   help="Write every derived keyword with a file count, from paths only, then exit")
    p.add_argument("--prune", action="store_true", help="Remove cache rows for files that no longer exist, then exit")
    p.add_argument("--yes", action="store_true", help="Allow --prune to remove more than 5%% of rows")
    args = p.parse_args()

    if args.root:
        SOURCE_DIR = Path(args.root).resolve()
    if not SOURCE_DIR.is_dir():
        sys.exit(f"Archive root not found: {SOURCE_DIR}")
    load_config(STATE_DIR)
    db_path = args.db or str(STATE_DIR / DB_FILE)
    log_path = args.log or str(STATE_DIR / LOG_FILE)

    if args.keywords_report:
        keywords_report(args.keywords_report)
        return
    _lock = acquire_lock()  # noqa: F841 (held until exit)
    if args.prune:
        prune_db(db_path, dry_run=args.dry_run, yes=args.yes)
    else:
        process_photos(db_path, log_path, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()