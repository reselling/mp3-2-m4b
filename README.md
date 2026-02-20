# mp3-2-m4b

Convert Libby audiobooks into a single `.m4b` file with chapters, cover art, and metadata — fast, with no re-encoding.

Built around a modified [LibreGRAB](https://github.com/PsychedelicPalimpsest/LibbyRip) Tampermonkey script and a local PyQt6 GUI app.

---

## How it works

1. You open an audiobook on [Libby](https://libbyapp.com) in your browser
2. The **LibreGRAB (M4B Edition)** Tampermonkey script adds an **🎧 Export M4B** button to the Libby player
3. Clicking it sends a job (metadata + signed MP3 URLs) to the local `gui.py` app over `localhost:7734`
4. The app downloads all the MP3 parts natively, builds chapter metadata, fetches cover art, and muxes everything into a single `.m4b` using ffmpeg — **no re-encoding**, stream copy only (~7,800× realtime)
5. Output is saved in Libation-style folder structure:

```
~/Music/Audiobooks/
└── Author Name/
    └── Book Title - Year/
        └── Book Title.m4b
```

---

## Requirements

- Python 3.11+
- [ffmpeg](https://ffmpeg.org) (must be in PATH)
- PyQt6

```bash
pip install PyQt6
brew install ffmpeg   # macOS
```

- [Tampermonkey](https://www.tampermonkey.net) browser extension

---

## Setup

### 1. Install the Tampermonkey script

1. Open Tampermonkey → Dashboard → **+** (Create new script)
2. Delete the placeholder code
3. Paste the full contents of `LibreGRAB-m4b.user.js`
4. Hit **Save**

### 2. Run the GUI app

```bash
python3 gui.py
```

Leave it running in the background while you use Libby. You can minimize it.

---

## Usage

1. Open an audiobook on [listen.libbyapp.com](https://listen.libbyapp.com)
2. You'll see a toolbar added by the script with four buttons:
   - **📖 Chapters** — preview audio players for each part
   - **⬇ MP3** — merge all parts into one MP3 (uses ffmpeg.wasm in-browser)
   - **🗂 ZIP export** — download all MP3 parts + metadata as a ZIP
   - **🎧 Export M4B** — send job to `gui.py` → outputs a fully-tagged `.m4b`
3. Click **🎧 Export M4B** — the panel will confirm the job was sent
4. Watch the `gui.py` window for download and conversion progress

> ⚠️ The `gui.py` app must be running before you click Export M4B, otherwise you'll get a red error message in the panel.

---

## Why M4B instead of MP3?

| | MP3 (in-browser) | M4B (via gui.py) |
|---|---|---|
| Speed | ~70× realtime | ~7,800× realtime |
| Chapters | ✅ | ✅ |
| Cover art | ✅ | ✅ |
| Metadata | ✅ | ✅ |
| Re-encoding | Yes (lossy) | **No** (stream copy) |
| Browser memory | High (50MB+ wasm) | None |
| File format | `.mp3` | `.m4b` (Apple Books, Overcast, etc.) |

The key trick: M4B is just an MP4 container. The `ipod` muxer rejects MP3 streams, but the `mp4` muxer accepts them (codec tag `mp4a.6B`). This means ffmpeg can stream-copy the audio directly — a 27-hour audiobook converts in about 10 seconds.

---

## Output folder structure

Matches [Libation](https://github.com/rmcrackan/Libation) conventions:

```
~/Music/Audiobooks/
└── Daniel Kahneman/
    └── Thinking, Fast and Slow - 2011/
        └── Thinking, Fast and Slow.m4b
```

You can change the output directory in the `gui.py` app.

---

## Files

| File | Description |
|------|-------------|
| `gui.py` | PyQt6 GUI app — HTTP server on :7734, downloads MP3s, runs ffmpeg |
| `LibreGRAB-m4b.user.js` | Tampermonkey userscript — adds M4B export button to Libby |

---

## Credits

- Original [LibreGRAB](https://github.com/PsychedelicPalimpsest/LibbyRip) by [PsychedelicPalimpsest](https://github.com/PsychedelicPalimpsest) — MIT License
- M4B export, modern UI, and local app integration by resellings
