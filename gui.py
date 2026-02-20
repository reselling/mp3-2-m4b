#!/usr/bin/env python3
"""
mp3-2-m4b GUI
─────────────
Listens on localhost:7734 for a job pushed by the Tampermonkey script,
then downloads the MP3 parts natively and muxes them into a single M4B.

Also supports drag-and-drop / folder-picker for already-downloaded ZIPs.

Run:
    python3 gui.py
"""

import json
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PyQt6.QtCore import (
    QMimeData, QObject, Qt, QThread, QUrl, pyqtSignal
)
from PyQt6.QtGui import QColor, QDragEnterEvent, QDropEvent, QPalette, QFont
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel,
    QMainWindow, QProgressBar, QPushButton, QSizePolicy,
    QTextEdit, QVBoxLayout, QWidget, QFrame
)

PORT = 7734
ACCENT = "#A61C49"
ACCENT_DARK = "#7a1436"
BG = "#121214"
SURFACE = "#1e1e22"
SURFACE2 = "#26262a"
BORDER = "#2a2a2e"
TEXT = "#f0f0f0"
MUTED = "#888"


# ─────────────────────────────────────────────
#  Helpers (shared with convert.py logic)
# ─────────────────────────────────────────────

def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)

def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", s)

def build_chapter_metadata(metadata: dict) -> str:
    spine = metadata["spine"]
    chapters = metadata.get("chapters", [])
    if not chapters:
        return ";FFMETADATA1\n"

    spine_starts = [0.0]
    for entry in spine[:-1]:
        spine_starts.append(spine_starts[-1] + entry["duration"])

    author = next((c["name"] for c in metadata.get("creator", []) if c["role"] == "author"), "")
    narrator = next((c["name"] for c in metadata.get("creator", []) if c["role"] == "narrator"), "")
    desc = strip_html(metadata.get("description", {}).get("short", "") if isinstance(metadata.get("description"), dict) else "")

    lines = [
        ";FFMETADATA1", "",
        f"title={metadata.get('title', '')}",
        f"artist={narrator}",
        f"album_artist={author}",
        f"album={metadata.get('title', '')}",
        "genre=Audiobook",
        f"comment={desc}", "",
    ]

    total_duration = spine_starts[-1] + spine[-1]["duration"]
    last_title = None
    filtered = []
    for ch in chapters:
        if ch["title"] != last_title:
            filtered.append(ch)
            last_title = ch["title"]

    for i, ch in enumerate(filtered):
        start_sec = spine_starts[ch["spine"]] + ch["offset"]
        if i + 1 < len(filtered):
            nxt = filtered[i + 1]
            end_sec = spine_starts[nxt["spine"]] + nxt["offset"]
        else:
            end_sec = total_duration
        lines += [
            "[CHAPTER]", "TIMEBASE=1/1000",
            f"START={int(start_sec * 1000)}",
            f"END={int(end_sec * 1000)}",
            f"title={ch['title']}", "",
        ]
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  Worker thread — download + mux
# ─────────────────────────────────────────────

class ConvertWorker(QThread):
    log      = pyqtSignal(str)          # append a log line
    progress = pyqtSignal(int, int)     # (current, total) for part downloads
    done     = pyqtSignal(str)          # output path on success
    error    = pyqtSignal(str)

    def __init__(self, job: dict, output_dir: Path):
        super().__init__()
        self.job = job
        self.output_dir = output_dir

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.error.emit(str(e))

    def _run(self):
        job = self.job
        metadata = job["metadata"]
        urls = job["urls"]          # list of {url, index} dicts
        title = metadata.get("title", "Audiobook")
        year  = str(metadata.get("year", "")).strip()
        author = next((c["name"] for c in metadata.get("creator", []) if c["role"] == "author"), "Unknown Author")

        self.log.emit(f"<b>📚 {title}</b>")
        self.log.emit(f"{len(urls)} parts  ·  {len(metadata.get('chapters', []))} chapters")

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mp3_paths = [None] * len(urls)

            # ── Download parts ──────────────────────────────
            self.log.emit("<br><b>⬇ Downloading parts…</b>")
            for i, part in enumerate(sorted(urls, key=lambda x: x["index"])):
                idx = part["index"]
                dest = tmp / f"{idx + 1:03d}.mp3"
                self.log.emit(f"  Part {idx + 1}/{len(urls)}…")
                self.progress.emit(i, len(urls))
                req = urllib.request.Request(part["url"], headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
                    while chunk := resp.read(65536):
                        f.write(chunk)
                mp3_paths[idx] = dest

            self.progress.emit(len(urls), len(urls))
            self.log.emit("<b style='color:#4caf50'>✓ All parts downloaded</b>")

            # ── Cover art ───────────────────────────────────
            cover_path = None
            cover_url = metadata.get("coverUrl")
            if cover_url:
                self.log.emit("<br><b>🖼 Fetching cover art…</b>")
                try:
                    req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
                    ext = cover_url.split(".")[-1].split("?")[0] or "jpg"
                    cover_dest = tmp / f"cover.{ext}"
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        cover_dest.write_bytes(resp.read())
                    cover_path = cover_dest
                    self.log.emit("✓ Cover art fetched")
                except Exception as e:
                    self.log.emit(f"⚠ Could not fetch cover: {e}")

            # ── Chapter metadata ────────────────────────────
            self.log.emit("<br><b>📋 Building chapter metadata…</b>")
            chap_content = build_chapter_metadata(metadata)
            chap_file = tmp / "chapters.txt"
            chap_file.write_text(chap_content, encoding="utf-8")

            # ── Concat list ─────────────────────────────────
            concat_file = tmp / "concat.txt"
            concat_file.write_text(
                "\n".join(f"file '{p}'" for p in mp3_paths if p),
                encoding="utf-8"
            )

            # ── ffmpeg mux ──────────────────────────────────
            self.log.emit("<br><b>⚙ Muxing to M4B…</b>")
            # Build Libation-style path: Author / Title - Year / Title.m4b
            folder_name = safe_name(f"{title} - {year}") if year else safe_name(title)
            out_folder = self.output_dir / safe_name(author) / folder_name
            out_folder.mkdir(parents=True, exist_ok=True)
            out = out_folder / f"{safe_name(title)}.m4b"

            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_file),
                "-i", str(chap_file),
            ]
            if cover_path:
                cmd += ["-i", str(cover_path)]
            cmd += ["-map_metadata", "1", "-map_chapters", "1", "-map", "0:a"]
            if cover_path:
                cmd += ["-map", "2:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
            cmd += ["-c:a", "copy", "-f", "mp4", "-movflags", "+faststart", str(out)]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                self.error.emit(result.stderr[-2000:])
                return

        self.log.emit(f"<br><b style='color:#4caf50'>✅ Done!</b>  →  {out}")
        self.done.emit(str(out))


# ─────────────────────────────────────────────
#  Local HTTP server (receives jobs from TM)
# ─────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    callback = None   # set by MainWindow

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            job = json.loads(body)
        except Exception:
            self.send_response(400)
            self._cors()
            self.end_headers()
            return
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')
        if self.callback:
            self.callback(job)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

    def log_message(self, *_):
        pass   # suppress server console spam


def start_server(callback):
    _Handler.callback = callback
    server = HTTPServer(("127.0.0.1", PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ─────────────────────────────────────────────
#  Drop zone widget
# ─────────────────────────────────────────────

class DropZone(QLabel):
    folder_dropped = pyqtSignal(Path)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setText("Drop extracted ZIP folder here\nor click to browse")
        self.setStyleSheet(f"""
            QLabel {{
                color: {MUTED};
                border: 2px dashed {BORDER};
                border-radius: 12px;
                padding: 32px;
                font-size: 14px;
            }}
            QLabel:hover {{
                border-color: {ACCENT};
                color: {TEXT};
            }}
        """)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(120)

    def mousePressEvent(self, _):
        folder = QFileDialog.getExistingDirectory(self, "Select audiobook folder")
        if folder:
            self.folder_dropped.emit(Path(folder))

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent):
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_dir():
                self.folder_dropped.emit(p)
                break


# ─────────────────────────────────────────────
#  Main window
# ─────────────────────────────────────────────

class MainWindow(QMainWindow):
    _job_received = pyqtSignal(dict)   # cross-thread signal

    def __init__(self):
        super().__init__()
        self.setWindowTitle("mp3 → m4b")
        self.setMinimumSize(600, 520)
        self._worker = None
        self._output_dir = Path.home() / "Music" / "Audiobooks"

        self._build_ui()
        self._job_received.connect(self._on_job)

        start_server(lambda job: self._job_received.emit(job))
        self._log(f"<span style='color:{MUTED}'>Listening on localhost:{PORT} — open a book in Libby and click <b>Export M4B</b></span>")

    # ── UI construction ──────────────────────────────────────

    def _build_ui(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; font-family: -apple-system, "Segoe UI", sans-serif; }}
            QTextEdit {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px;
                         color: {TEXT}; font-size: 13px; padding: 8px; }}
            QProgressBar {{ background: {SURFACE2}; border: none; border-radius: 4px; height: 6px; text-align: center; }}
            QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
            QPushButton {{
                background: {SURFACE2}; color: {TEXT}; border: 1px solid {BORDER};
                border-radius: 8px; padding: 8px 16px; font-size: 13px; font-weight: 500;
            }}
            QPushButton:hover {{ background: {ACCENT}; border-color: {ACCENT}; }}
            QPushButton#primary {{
                background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600;
            }}
            QPushButton#primary:hover {{ background: {ACCENT_DARK}; border-color: {ACCENT_DARK}; }}
            QPushButton:disabled {{ opacity: 0.4; }}
        """)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        # ── Header ──
        header = QLabel("mp3 → m4b")
        header.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {TEXT};")
        sub = QLabel("Libby audiobook converter")
        sub.setStyleSheet(f"font-size: 13px; color: {MUTED}; margin-top: -6px;")
        layout.addWidget(header)
        layout.addWidget(sub)

        # ── Status pill ──
        self._status = QLabel("● Waiting for Libby…")
        self._status.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        layout.addWidget(self._status)

        # ── Drop zone ──
        self._drop = DropZone()
        self._drop.folder_dropped.connect(self._on_folder)
        layout.addWidget(self._drop)

        # ── Progress bar ──
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        layout.addWidget(self._bar)

        # ── Log ──
        self._log_box = QTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMinimumHeight(180)
        layout.addWidget(self._log_box)

        # ── Bottom row ──
        row = QHBoxLayout()
        row.setSpacing(8)

        self._out_btn = QPushButton(f"📁  Output: {self._output_dir}")
        self._out_btn.clicked.connect(self._pick_output)
        self._out_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self._out_btn)

        self._reveal_btn = QPushButton("Show in Finder")
        self._reveal_btn.setEnabled(False)
        self._reveal_btn.clicked.connect(self._reveal)
        row.addWidget(self._reveal_btn)

        layout.addLayout(row)
        self._last_output = None

    # ── Slots ────────────────────────────────────────────────

    def _log(self, html: str):
        self._log_box.append(html)
        self._log_box.verticalScrollBar().setValue(
            self._log_box.verticalScrollBar().maximum()
        )

    def _on_job(self, job: dict):
        """Called (on the main thread) when TM pushes a job."""
        if self._worker and self._worker.isRunning():
            self._log("<span style='color:orange'>⚠ Already running — please wait.</span>")
            return
        self._status.setText("● Downloading…")
        self._status.setStyleSheet(f"color: {ACCENT}; font-size: 12px; font-weight: 600;")
        self._bar.setValue(0)
        self._reveal_btn.setEnabled(False)
        self._log("<hr>")
        self._start_worker(job)

    def _on_folder(self, folder: Path):
        """Convert an already-downloaded folder (drag/drop or picker)."""
        meta_path = folder / "metadata" / "metadata.json"
        if not meta_path.exists():
            self._log(f"<span style='color:orange'>⚠ No metadata/metadata.json found in {folder}</span>")
            return
        with open(meta_path, encoding="utf-8") as f:
            metadata = json.load(f)

        mp3s = sorted(folder.glob("*.mp3"))
        if not mp3s:
            self._log("<span style='color:orange'>⚠ No MP3 files found in folder.</span>")
            return

        # Build fake job with local file:// URLs
        urls = [{"url": p.as_uri(), "index": i} for i, p in enumerate(mp3s)]
        self._on_job({"metadata": metadata, "urls": urls, "local": True})

    def _start_worker(self, job: dict):
        self._worker = ConvertWorker(job, self._output_dir)
        total = len(job["urls"])
        self._bar.setRange(0, total)
        self._worker.log.connect(self._log)
        self._worker.progress.connect(lambda cur, tot: self._bar.setValue(cur))
        self._worker.done.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_done(self, path: str):
        self._last_output = path
        self._status.setText("● Done")
        self._status.setStyleSheet(f"color: #4caf50; font-size: 12px; font-weight: 600;")
        self._bar.setValue(self._bar.maximum())
        self._reveal_btn.setEnabled(True)

    def _on_error(self, msg: str):
        self._log(f"<span style='color:#f44336'>❌ Error:<br><pre>{msg}</pre></span>")
        self._status.setText("● Error")
        self._status.setStyleSheet("color: #f44336; font-size: 12px; font-weight: 600;")

    def _pick_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose output folder", str(self._output_dir))
        if folder:
            self._output_dir = Path(folder)
            self._out_btn.setText(f"📁  Output: {self._output_dir}")

    def _reveal(self):
        if self._last_output:
            subprocess.run(["open", "-R", self._last_output])


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("mp3-2-m4b")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
