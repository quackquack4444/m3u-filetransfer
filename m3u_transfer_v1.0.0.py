import os
import shutil
import sys
import time


# =============================================================
# Utility functions
# =============================================================

def format_bytes(num_bytes):
    """Format bytes as a human-readable size."""
    units = ["B", "KB", "MB", "GB", "TB"]

    size = float(num_bytes)

    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"

        size /= 1024

    return f"{size:.2f} TB"


def format_rate(bytes_per_second):
    """Format a transfer rate."""
    return f"{format_bytes(bytes_per_second)}/s"


def format_elapsed(seconds):
    """Format elapsed time as HH:MM:SS or MM:SS."""
    seconds = int(seconds)

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    return f"{minutes:02d}:{seconds:02d}"


# =============================================================
# Main function
# =============================================================

def copy_from_m3u(
    m3u_file,
    source_base,
    destination,
    verbose=False,
    time_offset=0.0
):

    source_base = os.path.abspath(source_base)
    destination = os.path.abspath(destination)
    playlist_dir = os.path.dirname(os.path.abspath(m3u_file))

    os.makedirs(destination, exist_ok=True)

    # ---------------------------------------------------------
    # PASS 1: Read the M3U and build the source -> destination
    #         file list.
    # ---------------------------------------------------------

    files_to_copy = []
    expected_files = set()

    with open(
        m3u_file,
        "r",
        encoding="utf-8-sig",
        errors="replace"
    ) as f:

        for line in f:

            line = line.strip()

            # Skip blank lines and M3U metadata
            if not line or line.startswith("#"):
                continue

            # Resolve source path
            if os.path.isabs(line):

                source = os.path.normpath(line)

            else:

                source = os.path.normpath(
                    os.path.join(
                        playlist_dir,
                        line
                    )
                )

            # -------------------------------------------------
            # Check source is actually inside source_base
            # -------------------------------------------------

            try:

                relative_path = os.path.relpath(
                    source,
                    source_base
                )

            except ValueError:

                print(
                    f"SKIPPED (different drive): {source}"
                )

                continue

            relative_compare = os.path.normcase(
                relative_path
            )

            if (
                relative_compare == os.pardir
                or relative_compare.startswith(
                    os.pardir + os.sep
                )
            ):

                print(
                    f"SKIPPED "
                    f"(outside source base path): {source}"
                )

                continue

            # -------------------------------------------------
            # Build destination path.
            #
            # Original case is preserved.
            # -------------------------------------------------

            target = os.path.normpath(
                os.path.join(
                    destination,
                    relative_path
                )
            )

            # Used only for case-insensitive comparison
            expected_files.add(
                os.path.normcase(
                    os.path.abspath(target)
                )
            )

            files_to_copy.append(
                (source, target)
            )

    total_tracks = len(files_to_copy)

    print(
        f"\nPlaylist contains "
        f"{total_tracks:,} track(s)."
    )

    if time_offset != 0:

        print(
            f"Timestamp comparison offset: "
            f"{time_offset:+g} hour(s)"
        )

    # =========================================================
    # PASS 2: Find files in destination which aren't in M3U
    #
    # IMPORTANT:
    # This happens BEFORE any copying/updating.
    # =========================================================

    print("\n" + "=" * 60)
    print("SCANNING DESTINATION")
    print("=" * 60)

    extra_files = []
    scanned_count = 0

    for root, dirs, files in os.walk(destination):

        for filename in files:

            full_path = os.path.abspath(
                os.path.join(
                    root,
                    filename
                )
            )

            scanned_count += 1

            if (
                os.path.normcase(full_path)
                not in expected_files
            ):

                extra_files.append(full_path)

            # Update every 500 files
            if (
                scanned_count == 1
                or scanned_count % 500 == 0
            ):

                print(
                    f"\rScanning destination... "
                    f"{scanned_count:,} files checked",
                    end="",
                    flush=True
                )

    print(
        f"\rScanning destination... "
        f"{scanned_count:,} files checked - complete"
    )

    # ---------------------------------------------------------
    # Handle files which aren't in the M3U
    # ---------------------------------------------------------

    if extra_files:

        print("\n" + "=" * 60)
        print("FILES IN DESTINATION NOT IN THE M3U")
        print("=" * 60)

        for file in extra_files:

            print(f"  {file}")

        print(
            f"\nFound {len(extra_files):,} file(s) "
            "not in the playlist."
        )

        while True:

            answer = input(
                "\nDelete these files BEFORE copying? [y/n]: "
            ).strip().lower()

            if answer in ("y", "yes"):

                print("\nDeleting files...")

                deleted_count = 0

                for file in extra_files:

                    try:

                        os.remove(file)

                        print(
                            f"DELETED: {file}"
                        )

                        deleted_count += 1

                    except OSError as e:

                        print(
                            f"FAILED: {file}"
                        )

                        print(
                            f"  {e}"
                        )

                print(
                    f"\nDeleted {deleted_count:,} "
                    f"of {len(extra_files):,} file(s)."
                )

                break

            elif answer in ("n", "no"):

                print(
                    "\nNo files were deleted."
                )

                break

            else:

                print(
                    "Please enter Y or N."
                )

    else:

        print(
            "\nNo files in the destination "
            "need removing."
        )

    # =========================================================
    # PASS 3: Copy missing / update changed files
    #
    # Comparison:
    #
    #   1. Different file size
    #   2. Source modification time is newer
    #
    # No hashing is performed.
    # =========================================================

    print("\n" + "=" * 60)
    print("COPYING / UPDATING TRACKS")
    print("=" * 60)

    copied_count = 0
    updated_count = 0
    unchanged_count = 0
    missing_count = 0
    failed_count = 0

    processed_count = 0

    total_bytes_transferred = 0

    # ---------------------------------------------------------
    # Timing
    # ---------------------------------------------------------

    transfer_start_time = time.monotonic()

    rate_start_time = transfer_start_time
    rate_start_bytes = 0

    current_rate = 0.0

    # Update recent rate every two seconds
    rate_interval = 2.0

    # ---------------------------------------------------------
    # Progress display
    # ---------------------------------------------------------

    bar_width = 32

    # Don't redraw the console for every unchanged file.
    display_interval = 0.20
    last_display_time = transfer_start_time

    # ---------------------------------------------------------
    # Calculate recent transfer rate
    # ---------------------------------------------------------

    def update_transfer_rate(force=False):

        nonlocal rate_start_time
        nonlocal rate_start_bytes
        nonlocal current_rate

        now = time.monotonic()

        elapsed = (
            now - rate_start_time
        )

        if force or elapsed >= rate_interval:

            bytes_since_update = (
                total_bytes_transferred
                - rate_start_bytes
            )

            if elapsed > 0:

                current_rate = (
                    bytes_since_update
                    / elapsed
                )

            rate_start_time = now
            rate_start_bytes = (
                total_bytes_transferred
            )

    # ---------------------------------------------------------
    # Display progress
    # ---------------------------------------------------------

    def show_progress(force=False):

        nonlocal last_display_time

        now = time.monotonic()

        # Throttle updates
        if (
            not force
            and now - last_display_time
            < display_interval
        ):

            return

        last_display_time = now

        update_transfer_rate()

        if total_tracks == 0:

            percent = 100.0

        else:

            percent = (
                processed_count
                / total_tracks
            ) * 100

        filled = int(
            bar_width
            * processed_count
            / max(total_tracks, 1)
        )

        bar = (
            "█" * filled
            + "░" * (bar_width - filled)
        )

        print(
            f"\r[{bar}] "
            f"{percent:6.2f}% "
            f"{processed_count:,}/{total_tracks:,}  "
            f"C:{copied_count:,} "
            f"U:{updated_count:,}  "
            f"Rate: {format_rate(current_rate):>12}",
            end="",
            flush=True
        )

    # ---------------------------------------------------------
    # Verbose message helper
    #
    # Clears the progress line before printing verbose output.
    # The progress bar is then redrawn underneath it.
    # ---------------------------------------------------------

    def verbose_message(message):

        if not verbose:
            return

        # Clear current progress line
        print(
            "\r" + (" " * 120) + "\r",
            end="",
            flush=True
        )

        print(message)

    # =========================================================
    # Process tracks
    # =========================================================

    for source, target in files_to_copy:

        changed_this_track = False

        # -----------------------------------------------------
        # Source doesn't exist
        # -----------------------------------------------------

        if not os.path.isfile(source):

            missing_count += 1

            verbose_message(
                f"NOT FOUND:\n"
                f"  {source}"
            )

        # -----------------------------------------------------
        # Destination doesn't exist
        # -----------------------------------------------------

        elif not os.path.exists(target):

            target_dir = os.path.dirname(target)

            try:

                os.makedirs(
                    target_dir,
                    exist_ok=True
                )

                source_size = os.path.getsize(
                    source
                )

                shutil.copy2(
                    source,
                    target
                )

                copied_count += 1

                total_bytes_transferred += (
                    source_size
                )

                changed_this_track = True

                verbose_message(
                    f"COPIED:\n"
                    f"  From: {source}\n"
                    f"  To:   {target}\n"
                    f"  Size: {format_bytes(source_size)}"
                )

            except OSError as e:

                failed_count += 1

                verbose_message(
                    f"FAILED:\n"
                    f"  {source}\n"
                    f"  {e}"
                )

        # -----------------------------------------------------
        # Destination exists
        # -----------------------------------------------------

        else:

            try:

                source_stat = os.stat(
                    source
                )

                target_stat = os.stat(
                    target
                )

                source_size = (
                    source_stat.st_size
                )

                target_size = (
                    target_stat.st_size
                )

                source_mtime = (
                    source_stat.st_mtime
                )

                target_mtime = (
                    target_stat.st_mtime
                )

            except OSError as e:

                failed_count += 1

                verbose_message(
                    f"FAILED TO CHECK:\n"
                    f"  {source}\n"
                    f"  {e}"
                )

                source_size = None
                target_size = None
                source_mtime = None
                target_mtime = None

            # -------------------------------------------------
            # Different size
            # -------------------------------------------------

            if (
                source_size is not None
                and source_size != target_size
            ):

                try:

                    shutil.copy2(
                        source,
                        target
                    )

                    updated_count += 1

                    total_bytes_transferred += (
                        source_size
                    )

                    changed_this_track = True

                    verbose_message(
                        f"UPDATED (different size):\n"
                        f"  From: {source}\n"
                        f"  To:   {target}\n"
                        f"  Size: "
                        f"{format_bytes(target_size)} -> "
                        f"{format_bytes(source_size)}"
                    )

                except OSError as e:

                    failed_count += 1

                    verbose_message(
                        f"FAILED:\n"
                        f"  {source}\n"
                        f"  {e}"
                    )

            # -------------------------------------------------
            # Same size but source is newer
            #
            # Apply timestamp offset ONLY to comparison.
            # -------------------------------------------------

            elif (
                source_size is not None
                and (
                    source_mtime
                    + (time_offset * 3600)
                    > target_mtime
                )
            ):

                try:

                    shutil.copy2(
                        source,
                        target
                    )

                    updated_count += 1

                    total_bytes_transferred += (
                        source_size
                    )

                    changed_this_track = True

                    verbose_message(
                        f"UPDATED (source newer):\n"
                        f"  From: {source}\n"
                        f"  To:   {target}\n"
                        f"  Size: {format_bytes(source_size)}"
                    )

                except OSError as e:

                    failed_count += 1

                    verbose_message(
                        f"FAILED:\n"
                        f"  {source}\n"
                        f"  {e}"
                    )

            # -------------------------------------------------
            # Unchanged
            # -------------------------------------------------

            elif source_size is not None:

                unchanged_count += 1

                if verbose:

                    verbose_message(
                        f"UNCHANGED:\n"
                        f"  {target}"
                    )

        # -----------------------------------------------------
        # Track complete
        # -----------------------------------------------------

        processed_count += 1

        # Force a display after a change.
        # Otherwise allow throttling.
        show_progress(
            force=changed_this_track
        )

    # ---------------------------------------------------------
    # Final rate calculation
    # ---------------------------------------------------------

    transfer_end_time = time.monotonic()

    transfer_elapsed = (
        transfer_end_time
        - transfer_start_time
    )

    update_transfer_rate(
        force=True
    )

    if transfer_elapsed > 0:

        average_rate = (
            total_bytes_transferred
            / transfer_elapsed
        )

    else:

        average_rate = 0.0

    # Ensure final progress is displayed
    show_progress(
        force=True
    )

    print()

    # =========================================================
    # Summary
    # =========================================================

    print("\n" + "=" * 60)
    print("FINISHED")
    print("=" * 60)

    print(
        f"Total tracks:     {total_tracks:,}"
    )

    print(
        f"Copied:           {copied_count:,}"
    )

    print(
        f"Updated:          {updated_count:,}"
    )

    print(
        f"Unchanged:        {unchanged_count:,}"
    )

    print(
        f"Missing:          {missing_count:,}"
    )

    print(
        f"Failed:           {failed_count:,}"
    )

    changed_count = (
        copied_count
        + updated_count
    )

    if total_tracks:

        changed_percent = (
            changed_count
            / total_tracks
        ) * 100

    else:

        changed_percent = 0.0

    print(
        f"Copied/updated:   {changed_count:,} "
        f"({changed_percent:.2f}%)"
    )

    print(
        f"Data transferred: "
        f"{format_bytes(total_bytes_transferred)}"
    )

    print(
        f"Average rate:     "
        f"{format_rate(average_rate)}"
    )

    print(
        f"Transfer time:    "
        f"{format_elapsed(transfer_elapsed)}"
    )


# =============================================================
# Command-line handling
# =============================================================

if __name__ == "__main__":

    args = sys.argv[1:]

    verbose = False
    time_offset = 0.0

    positional_args = []

    i = 0

    while i < len(args):

        arg = args[i]

        # -----------------------------------------------------
        # Verbose
        # -----------------------------------------------------

        if arg.lower() == "--verbose":

            verbose = True

        # -----------------------------------------------------
        # Timestamp offset
        # -----------------------------------------------------

        elif arg.lower() == "--offset":

            if i + 1 >= len(args):

                print(
                    "ERROR: --offset requires "
                    "a number of hours."
                )

                sys.exit(1)

            try:

                time_offset = float(
                    args[i + 1]
                )

            except ValueError:

                print(
                    "ERROR: --offset must be "
                    "a number of hours."
                )

                sys.exit(1)

            i += 1

        # -----------------------------------------------------
        # Unknown option
        # -----------------------------------------------------

        elif arg.startswith("--"):

            print(
                f"ERROR: Unknown option: {arg}"
            )

            print()
            print(
                "Available options:"
            )

            print(
                "  --verbose"
            )

            print(
                "  --offset HOURS"
            )

            sys.exit(1)

        # -----------------------------------------------------
        # Positional argument
        # -----------------------------------------------------

        else:

            positional_args.append(arg)

        i += 1

    # =========================================================
    # Validate arguments
    # =========================================================

    if len(positional_args) != 3:

        print("Usage:")

        print(
            '  python copy_m3u.py '
            '"playlist.m3u" '
            '"source_base_path" '
            '"destination_folder" '
            '[--verbose] '
            '[--offset HOURS]'
        )

        print()
        print("Examples:")

        print(
            '  python copy_m3u.py '
            '"playlist.m3u" '
            '"M:\\Music\\Music" '
            '"D:\\sync"'
        )

        print()

        print(
            '  python copy_m3u.py '
            '"playlist.m3u" '
            '"M:\\Music\\Music" '
            '"D:\\sync" '
            '--verbose'
        )

        print()

        print(
            '  python copy_m3u.py '
            '"playlist.m3u" '
            '"M:\\Music\\Music" '
            '"D:\\sync" '
            '--offset 1'
        )

        print()

        print(
            '  python copy_m3u.py '
            '"playlist.m3u" '
            '"M:\\Music\\Music" '
            '"D:\\sync" '
            '--verbose '
            '--offset 1'
        )

        sys.exit(1)

    m3u_file = positional_args[0]
    source_base = positional_args[1]
    destination = positional_args[2]

    # =========================================================
    # Run
    # =========================================================

    copy_from_m3u(
        m3u_file,
        source_base,
        destination,
        verbose=verbose,
        time_offset=time_offset
    )
