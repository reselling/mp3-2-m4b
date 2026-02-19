#!/usr/bin/env python3
"""
Convert Libby-ripped MP3 audiobooks into a single M4B file with chapters.

Usage:
    python convert.py <audiobook_dir> [output_dir]

The audiobook_dir must contain:
  - Part NNN.mp3 files (named to sort in order)
  - metadata/metadata.json  (Libby format)

If output_dir is omitted, the M4B is placed in the current directory.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, **kwargs):
    """Run a subprocess, print the command, raise on failure."""
    print("  $", " ".join(str(c) for c in cmd))
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        sys.exit(f"Command failed with exit code {result.returncode}")
    return result


def format_time(seconds: float) -> str:
    """Convert seconds to HH:MM:SS.mmm for ffmpeg chapter metadata."""
    ms = round(seconds * 1000)
    h, remainder = divmod(ms, 3_600_000)
    m, remainder = divmod(remainder, 60_000)
    s, ms = divmod(remainder, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def strip_html(text: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def find_mp3s(book_dir: Path) -> list[Path]:
    """Return sorted list of MP3 files in book_dir."""
    mp3s = sorted(book_dir.glob("*.mp3"))
    if not mp3s:
        sys.exit(f"No MP3 files found in {book_dir}")
    return mp3s


def load_metadata(book_dir: Path) -> dict:
    meta_path = book_dir / "metadata" / "metadata.json"
    if not meta_path.exists():
        sys.exit(f"metadata.json not found at {meta_path}")
    with open(meta_path, encoding="utf-8") as f:
        return json.load(f)


def download_cover(url: str, dest: Path) -> bool:
    """Download cover art; return True on success."""
    try:
        print(f"  Downloading cover art from {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            dest.write_bytes(resp.read())
        return True
    except Exception as e:
        print(f"  Warning: could not download cover art: {e}")
        return False


def build_chapter_metadata(metadata: dict, mp3s: list[Path]) -> str:
    """
    Build the ffmpeg metadata file content with chapter markers.

    The Libby JSON uses:
      chapter.spine  -> index into the spine array (= MP3 file index)
      chapter.offset -> seconds from the start of that spine file

    We accumulate the running start time of each spine file so we can
    compute absolute timestamps for every chapter.
    """
    spine = metadata["spine"]
    chapters = metadata["chapters"]

    # Cumulative start time (in seconds) for each spine file
    spine_starts = [0.0]
    for entry in spine[:-1]:
        spine_starts.append(spine_starts[-1] + entry["duration"])

    lines = [
        ";FFMETADATA1",
        "",
        # Book-level tags
        f"title={metadata.get('title', '')}",
        f"artist={next((c['name'] for c in metadata.get('creator', []) if c['role'] == 'narrator'), '')}",
        f"album_artist={next((c['name'] for c in metadata.get('creator', []) if c['role'] == 'author'), '')}",
        f"album={metadata.get('title', '')}",
        f"genre=Audiobook",
        f"comment={strip_html(metadata.get('description', {}).get('short', ''))}",
        "",
    ]

    # Total duration (seconds) – used to close the last chapter
    total_duration = spine_starts[-1] + spine[-1]["duration"]

    for i, ch in enumerate(chapters):
        spine_idx = ch["spine"]
        offset = ch["offset"]  # seconds within that spine file

        start_sec = spine_starts[spine_idx] + offset

        # End is the start of the next chapter, or the total duration
        if i + 1 < len(chapters):
            next_ch = chapters[i + 1]
            end_sec = spine_starts[next_ch["spine"]] + next_ch["offset"]
        else:
            end_sec = total_duration

        # ffmpeg chapter timestamps are in milliseconds (timebase 1/1000)
        start_ms = int(start_sec * 1000)
        end_ms = int(end_sec * 1000)

        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={end_ms}",
            f"title={ch['title']}",
            "",
        ]

    return "\n".join(lines)


def convert(book_dir: Path, output_dir: Path):
    print(f"\n=== Converting: {book_dir.name} ===\n")

    mp3s = find_mp3s(book_dir)
    print(f"Found {len(mp3s)} MP3 file(s):")
    for p in mp3s:
        print(f"  {p.name}")

    metadata = load_metadata(book_dir)
    title = metadata.get("title", book_dir.name)
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # --- 1. Concatenate MP3s into a single intermediate file ------------
        concat_list = tmp / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{p}'" for p in mp3s), encoding="utf-8"
        )

        merged_mp3 = tmp / "merged.mp3"
        print("\n[1/4] Concatenating MP3 files...")
        run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(merged_mp3),
        ], capture_output=True)

        # --- 2. Download cover art -------------------------------------------
        cover_path = None
        cover_url = metadata.get("coverUrl")
        if cover_url:
            print("\n[2/4] Fetching cover art...")
            candidate = tmp / "cover.jpg"
            if download_cover(cover_url, candidate):
                cover_path = candidate

        # --- 3. Write ffmpeg chapter metadata file ---------------------------
        print("\n[3/4] Building chapter metadata...")
        chap_meta = build_chapter_metadata(metadata, mp3s)
        meta_file = tmp / "chapters.txt"
        meta_file.write_text(chap_meta, encoding="utf-8")

        # --- 4. Encode to M4B ------------------------------------------------
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{safe_title}.m4b"

        print(f"\n[4/4] Encoding to M4B → {output_file}")

        cmd = [
            "ffmpeg", "-y",
            "-i", str(merged_mp3),
            "-i", str(meta_file),
        ]

        if cover_path:
            cmd += ["-i", str(cover_path)]

        # Map audio from merged mp3, metadata from chapter file
        cmd += [
            "-map_metadata", "1",
            "-map_chapters", "1",
            "-map", "0:a",
        ]

        if cover_path:
            cmd += [
                "-map", "2:v",
                "-c:v", "copy",
                "-disposition:v", "attached_pic",
            ]

        cmd += [
            "-c:a", "aac",
            "-b:a", "64k",
            "-movflags", "+faststart",
            str(output_file),
        ]

        run(cmd)

    print(f"\nDone! Output: {output_file}")
    return output_file


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert a Libby-ripped MP3 audiobook folder to a single M4B."
    )
    parser.add_argument(
        "book_dir",
        help="Path to the audiobook folder (contains Part NNN.mp3 + metadata/)",
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=".",
        help="Directory where the .m4b file will be saved (default: current dir)",
    )
    args = parser.parse_args()

    book_dir = Path(args.book_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not book_dir.is_dir():
        sys.exit(f"Error: '{book_dir}' is not a directory.")

    convert(book_dir, output_dir)


if __name__ == "__main__":
    main()
