#!/usr/bin/env python3
"""
copy_m3u.py

Sync the audio files referenced by an M3U playlist from a source tree into
a destination tree, mirroring the playlist's relative folder structure.
Destination files that already match the source (same size, not older) are
left untouched. No hashing is performed - comparison is by size and mtime.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass


# ===========================================================================
# Formatting helpers
# ===========================================================================

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_bytes(num_bytes: float) -> str:
    """Format a byte count as a human-readable size, e.g. '12.34 MB'."""
    size = float(num_bytes)
    for unit in _SIZE_UNITS[:-1]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} {_SIZE_UNITS[-1]}"


def format_rate(bytes_per_second: float) -> str:
    return f"{format_bytes(bytes_per_second)}/s"


def format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as HH:MM:SS or MM:SS."""
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


# ===========================================================================
# Playlist parsing
# ===========================================================================

def read_playlist(m3u_file: str, source_base: str) -> list[tuple[str, str]]:
    """
    Parse an M3U file and return (source, relative_path) pairs for every
    entry that resolves to somewhere inside `source_base`. Entries outside
    of it (or on a different drive) are skipped with a warning and excluded.
    """
    playlist_dir = os.path.dirname(os.path.abspath(m3u_file))
    pairs: list[tuple[str, str]] = []

    with open(m3u_file, "r", encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            source = line if os.path.isabs(line) else os.path.join(playlist_dir, line)
            source = os.path.normpath(source)

            try:
                relative = os.path.relpath(source, source_base)
            except ValueError:
                print(f"SKIPPED (different drive): {source}")
                continue

            normalized = os.path.normcase(relative)
            if normalized == os.pardir or normalized.startswith(os.pardir + os.sep):
                print(f"SKIPPED (outside source base path): {source}")
                continue

            pairs.append((source, relative))

    return pairs


# ===========================================================================
# Destination scan - files that exist but aren't referenced by the playlist
# ===========================================================================

def find_extra_files(destination: str, expected: set[str]) -> list[str]:
    """Walk `destination` and return absolute paths not present in `expected`."""
    extras: list[str] = []
    scanned = 0

    for root, _dirs, files in os.walk(destination):
        for filename in files:
            full_path = os.path.abspath(os.path.join(root, filename))
            scanned += 1

            if os.path.normcase(full_path) not in expected:
                extras.append(full_path)

            if scanned == 1 or scanned % 500 == 0:
                print(f"\rScanning destination... {scanned:,} files checked", end="", flush=True)

    print(f"\rScanning destination... {scanned:,} files checked - complete")
    return extras


def prompt_delete_extras(extra_files: list[str]) -> None:
    print("\n" + "=" * 60)
    print("FILES IN DESTINATION NOT IN THE M3U")
    print("=" * 60)
    for file in extra_files:
        print(f"  {file}")
    print(f"\nFound {len(extra_files):,} file(s) not in the playlist.")

    while True:
        answer = input("\nDelete these files BEFORE copying? [y/n]: ").strip().lower()

        if answer in ("y", "yes"):
            print("\nDeleting files...")
            deleted = 0
            for file in extra_files:
                try:
                    os.remove(file)
                    print(f"DELETED: {file}")
                    deleted += 1
                except OSError as e:
                    print(f"FAILED: {file}\n  {e}")
            print(f"\nDeleted {deleted:,} of {len(extra_files):,} file(s).")
            return

        if answer in ("n", "no"):
            print("\nNo files were deleted.")
            return

        print("Please enter Y or N.")


# ===========================================================================
# Per-file sync
# ===========================================================================

@dataclass
class SyncResult:
    action: str  # "copied" | "updated" | "unchanged" | "missing" | "failed"
    bytes_transferred: int = 0
    detail: str = ""  # human-readable message, shown only with --verbose


_COPY_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB - large enough to be efficient on slow media


def copy_file(source: str, target: str, fsync: bool = True) -> None:
    """
    Copy `source` to `target`, preserving metadata like shutil.copy2().

    When `fsync` is True, the destination file is flushed and fsync'd before
    this function returns, so the data is actually on the storage device -
    not just handed to the OS's write cache - before we report it as done or
    move on to the next file. Without this, a slow device (e.g. an SD card)
    can fall far behind what the progress bar shows, and interrupting the
    script (Ctrl-C) can leave writes still draining to disk afterwards.
    """
    with open(source, "rb") as src, open(target, "wb") as dst:
        while True:
            chunk = src.read(_COPY_CHUNK_SIZE)
            if not chunk:
                break
            dst.write(chunk)

        if fsync:
            dst.flush()
            os.fsync(dst.fileno())

    shutil.copystat(source, target)


def sync_one(source: str, target: str, time_offset_hours: float, fsync: bool = True) -> SyncResult:
    """Copy or update a single file. Never raises - errors come back as 'failed'."""

    if not os.path.isfile(source):
        return SyncResult("missing", detail=f"NOT FOUND:\n  {source}")

    if not os.path.exists(target):
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            size = os.path.getsize(source)
            copy_file(source, target, fsync=fsync)
            return SyncResult(
                "copied", size,
                f"COPIED:\n  From: {source}\n  To:   {target}\n  Size: {format_bytes(size)}",
            )
        except OSError as e:
            return SyncResult("failed", detail=f"FAILED:\n  {source}\n  {e}")

    try:
        source_stat = os.stat(source)
        target_stat = os.stat(target)
    except OSError as e:
        return SyncResult("failed", detail=f"FAILED TO CHECK:\n  {source}\n  {e}")

    size_changed = source_stat.st_size != target_stat.st_size
    source_is_newer = (source_stat.st_mtime + time_offset_hours * 3600) > target_stat.st_mtime

    if size_changed or source_is_newer:
        try:
            copy_file(source, target, fsync=fsync)
        except OSError as e:
            return SyncResult("failed", detail=f"FAILED:\n  {source}\n  {e}")

        reason = "different size" if size_changed else "source newer"
        return SyncResult(
            "updated", source_stat.st_size,
            f"UPDATED ({reason}):\n  From: {source}\n  To:   {target}\n"
            f"  Size: {format_bytes(target_stat.st_size)} -> {format_bytes(source_stat.st_size)}",
        )

    return SyncResult("unchanged", detail=f"UNCHANGED:\n  {target}")


# ===========================================================================
# Progress reporting
# ===========================================================================

class ProgressReporter:
    """Tracks counts/bytes as results come in and prints a single-line progress bar."""

    def __init__(self, total: int, bar_width: int = 32,
                 rate_interval: float = 2.0, display_interval: float = 0.2):
        self.total = total
        self.bar_width = bar_width
        self.rate_interval = rate_interval
        self.display_interval = display_interval

        self.processed = 0
        self.copied = 0
        self.updated = 0
        self.unchanged = 0
        self.missing = 0
        self.failed = 0
        self.bytes_transferred = 0

        now = time.monotonic()
        self.start_time = now
        self._rate_time = now
        self._rate_bytes = 0
        self.current_rate = 0.0
        self._last_display = now

    def record(self, result: SyncResult) -> None:
        self.processed += 1
        self.bytes_transferred += result.bytes_transferred
        if result.action in ("copied", "updated", "unchanged", "missing", "failed"):
            setattr(self, result.action, getattr(self, result.action) + 1)

    def _update_rate(self, force: bool = False) -> None:
        now = time.monotonic()
        elapsed = now - self._rate_time
        if force or elapsed >= self.rate_interval:
            if elapsed > 0:
                self.current_rate = (self.bytes_transferred - self._rate_bytes) / elapsed
            self._rate_time = now
            self._rate_bytes = self.bytes_transferred

    def show(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_display < self.display_interval:
            return
        self._last_display = now

        self._update_rate()

        percent = 100.0 if self.total == 0 else (self.processed / self.total) * 100
        filled = int(self.bar_width * self.processed / max(self.total, 1))
        bar = "█" * filled + "░" * (self.bar_width - filled)

        print(
            f"\r[{bar}] {percent:6.2f}% {self.processed:,}/{self.total:,}  "
            f"C:{self.copied:,} U:{self.updated:,}  Rate: {format_rate(self.current_rate):>12}",
            end="", flush=True,
        )

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time


# ===========================================================================
# Main sync driver
# ===========================================================================

def print_summary(total_tracks: int, progress: "ProgressReporter") -> None:
    print("\n" + "=" * 60)
    print("FINISHED")
    print("=" * 60)

    print(f"Total tracks:     {total_tracks:,}")
    print(f"Copied:           {progress.copied:,}")
    print(f"Updated:          {progress.updated:,}")
    print(f"Unchanged:        {progress.unchanged:,}")
    print(f"Missing:          {progress.missing:,}")
    print(f"Failed:           {progress.failed:,}")

    changed_count = progress.copied + progress.updated
    changed_percent = (changed_count / total_tracks * 100) if total_tracks else 0.0
    print(f"Copied/updated:   {changed_count:,} ({changed_percent:.2f}%)")

    print(f"Data transferred: {format_bytes(progress.bytes_transferred)}")
    average_rate = progress.bytes_transferred / progress.elapsed if progress.elapsed > 0 else 0.0
    print(f"Average rate:     {format_rate(average_rate)}")
    print(f"Transfer time:    {format_elapsed(progress.elapsed)}")


def copy_from_m3u(
    m3u_file: str,
    source_base: str,
    destination: str,
    verbose: bool = False,
    time_offset: float = 0.0,
    workers: int = 1,
    fsync: bool = True,
) -> None:
    source_base = os.path.abspath(source_base)
    destination = os.path.abspath(destination)
    os.makedirs(destination, exist_ok=True)

    # -- Pass 1: read the playlist, resolve every entry to an absolute target --
    relative_pairs = read_playlist(m3u_file, source_base)
    files_to_copy = [
        (source, os.path.normpath(os.path.join(destination, relative)))
        for source, relative in relative_pairs
    ]
    expected = {os.path.normcase(target) for _, target in files_to_copy}

    total_tracks = len(files_to_copy)
    print(f"\nPlaylist contains {total_tracks:,} track(s).")
    if time_offset:
        print(f"Timestamp comparison offset: {time_offset:+g} hour(s)")

    # -- Pass 2: find destination files that aren't in the playlist, BEFORE copying --
    print("\n" + "=" * 60)
    print("SCANNING DESTINATION")
    print("=" * 60)

    extra_files = find_extra_files(destination, expected)
    if extra_files:
        prompt_delete_extras(extra_files)
    else:
        print("\nNo files in the destination need removing.")

    # -- Pass 3: copy / update tracks --
    print("\n" + "=" * 60)
    print("COPYING / UPDATING TRACKS")
    print("=" * 60)

    progress = ProgressReporter(total_tracks)

    def verbose_message(message: str) -> None:
        if not verbose:
            return
        print("\r" + " " * 120 + "\r", end="", flush=True)  # clear the progress line
        print(message)

    def handle(result: SyncResult) -> None:
        progress.record(result)
        verbose_message(result.detail)
        progress.show(force=result.action in ("copied", "updated"))

    try:
        if workers > 1 and total_tracks > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(sync_one, source, target, time_offset, fsync)
                    for source, target in files_to_copy
                ]
                try:
                    for future in as_completed(futures):
                        handle(future.result())
                except KeyboardInterrupt:
                    # Stop handing out new work immediately. Files already being
                    # copied by a worker thread are left to finish (so we don't
                    # leave a half-written file behind); anything not yet started
                    # is cancelled outright rather than run to completion.
                    for f in futures:
                        f.cancel()
                    pool.shutdown(wait=True, cancel_futures=True)
                    raise
        else:
            for source, target in files_to_copy:
                handle(sync_one(source, target, time_offset, fsync))
    except KeyboardInterrupt:
        progress.show(force=True)
        print("\n\nStopped by user.")
        if fsync:
            print("(--fsync was on, so every file reported above is fully written to disk.)")
        print_summary(total_tracks, progress)
        sys.exit(130)

    progress.show(force=True)
    print()

    print_summary(total_tracks, progress)


# ===========================================================================
# Command-line interface
# ===========================================================================

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy/update the files referenced by an M3U playlist into a destination folder.",
        epilog=(
            'Example: python copy_m3u.py "playlist.m3u" "M:\\Music\\Music" "D:\\sync" '
            "--verbose --offset 1 --workers 4"
        ),
    )
    parser.add_argument("m3u_file", help="Path to the .m3u playlist")
    parser.add_argument("source_base", help="Root folder that playlist entries are relative to")
    parser.add_argument("destination", help="Folder to copy/update files into")
    parser.add_argument("--verbose", action="store_true",
                         help="Print a line for every file processed")
    parser.add_argument("--offset", type=float, default=0.0, metavar="HOURS",
                         help="Hours added to the source's mtime before comparing it against "
                              "the destination (for clock/timezone differences). Default: 0")
    parser.add_argument("--workers", type=int, default=1, metavar="N",
                         help="Copy this many files concurrently. Try 4-8 for network drives "
                              "or many small files. Default: 1 (serial, matches original behavior)")
    parser.add_argument("--no-fsync", action="store_true",
                         help="Don't force each file to disk before moving on (default: fsync "
                              "every file). Faster, but on slow media (SD cards, USB drives) the "
                              "OS may still be writing to the card well after the script exits, "
                              "and Ctrl-C can leave writes queued instead of stopping cleanly.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])

    if args.workers < 1:
        print("ERROR: --workers must be at least 1.")
        sys.exit(1)

    copy_from_m3u(
        args.m3u_file,
        args.source_base,
        args.destination,
        verbose=args.verbose,
        time_offset=args.offset,
        workers=args.workers,
        fsync=not args.no_fsync,
    )
