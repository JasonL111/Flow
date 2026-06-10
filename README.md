# ◆ Flow

<p align="center">
  <strong>GPU-Accelerated Video Pipeline — Sort, Filter, Compress, Index.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/codec-AV1-brightgreen" alt="AV1">
  <img src="https://img.shields.io/badge/accel-VAAPI-orange" alt="VAAPI">
  <img src="https://img.shields.io/badge/interface-TUI%20%26%20Web-9cf" alt="Interface">
</p>

---

Flow is a lightweight, zero-dependency Python tool for managing media directories. It organizes files by extension, quarantines low-quality clips, transcodes video to **AV1** using hardware-accelerated **VAAPI**, and maintains **BLAKE3** content-addressable indexes. Dual interfaces — a curses TUI and a modern Web UI with real-time SSE progress — make it equally at home on a headless server or your desktop.

## Features

| Step | Action | Description |
|------|--------|-------------|
| ① | **Sort** | Moves files into `MP4/`, `LRF/`, or `OTHER/` by extension |
| ② | **Filter** | Quarantines corrupt or sub-2s videos into `LOW_QUALITY/` |
| ③ | **Compress** | GPU AV1 transcoding with color metadata passthrough & HDR awareness |
| ④ | **Index** | Generates a BLAKE3 content-index for integrity and dedup |

- **Hardware AV1 encoding** via AMD VAAPI (`av1_vaapi`)
- **HDR / 10-bit** color preservation (PQ, HLG, BT.2020)
- **Output validation** — duration drift check catches silent encoder corruption
- **Size-gate** — skips re-encoding when savings would be < 10%
- **Duplicate detection** by BLAKE3 hash before overwriting
- **Dry-run mode** previews all changes without touching files
- **Bilingual** Web UI (EN / 中文)

## Prerequisites

| Tool | Why |
|------|-----|
| [`ffmpeg`](https://ffmpeg.org/) + `ffprobe` | Media probe, AV1 transcode |
| [`b3sum`](https://github.com/BLAKE3-team/BLAKE3) | BLAKE3 cryptographic hashing |
| AMD GPU with VAAPI | `/dev/dri/renderD128` hardware encoder |
| Python ≥ 3.10 | Runtime |

Install system dependencies on Ubuntu/Debian:

```bash
sudo apt install ffmpeg b3sum
```

On Arch:

```bash
sudo pacman -S ffmpeg b3sum
```

## Quick Start

```bash
# TUI (interactive menu)
python3 album_tui.py --path /mnt/drone_footage

# Run the full pipeline (non-interactive)
python3 album_tui.py --path /mnt/drone_footage all \
  --keep-original \
  --codec-mode non_av1

# Web UI
python3 webui.py --root /mnt/drone_footage --port 8080
# → open http://127.0.0.1:8080
```

## Usage

### Interactive TUI

```
python3 album_tui.py [--path DIR] [--device /dev/dri/renderD128] [--dry-run]
```

Navigate with `↑↓` / `jk`, select with `Enter`, quit with `q`.

| Key | Action |
|-----|--------|
| `↑/↓` or `j/k` | Move selection |
| `Enter` | Run selected action |
| `r` | Change root directory |
| `q` / `Esc` | Quit |

### CLI Batch Mode

```
album_tui.py CMD [options]
```

| Command | Description |
|---------|-------------|
| `tui` | Launch interactive TUI (default) |
| `sort` | Sort files by extension |
| `filter` | Quarantine low-quality videos |
| `compress` | Transcode MP4 to AV1 via VAAPI |
| `index` | Sync BLAKE3 index |
| `all` | Full 4-step pipeline |

#### Common flags

| Flag | Description |
|------|-------------|
| `--path`, `--root` | Target directory (default `.`) |
| `--device` | VAAPI render node (default `/dev/dri/renderD128`) |
| `--dry-run` | Preview without modification |
| `--keep-original` | Retain source files after compression |
| `--codec-mode` | `non_av1` (default) or `hevc_only` |
| `--compress-source` | Override source dir for compression |
| `--compress-output` | Override output dir for compression |
| `--index-file` | BLAKE3 index path |
| `--index-scan-root` | Directory to scan for indexing |

### Web UI

```bash
python3 webui.py --root /path/to/media [--port 8080]
```

- Dark-themed SPA with sidebar navigation
- Real-time progress via **Server-Sent Events**
- Configuration dialogs for compress, index, and pipeline settings
- Log viewer with color-coded entries
- Cancel button for live tasks

## Pipeline

```
  ┌────────┐    ┌──────────┐    ┌───────────┐    ┌─────────┐
  │  Sort  │ →  │  Filter  │ →  │ Compress  │ →  │  Index  │
  │ by ext │    │ low qual │    │ VAAPI AV1 │    │ BLAKE3  │
  └────────┘    └──────────┘    └───────────┘    └─────────┘
```

### Directory Layout After Pipeline

```
media_root/
├── MP4/                  ← .mp4 files sorted here
│   ├── clip1.MP4
│   └── clip2.MP4
├── MP4_C/                ← compressed output
│   ├── MP4_C.b3          ← BLAKE3 index
│   ├── clip1_av1.MP4     ← successfully AV1-compressed
│   └── clip2_oth.MP4     ← kept as-is (wouldn't shrink)
├── LRF/                  ← .lrf (DJI proxy) files
├── OTHER/                ← all other extensions
└── LOW_QUALITY/          ← short / corrupt videos
```

### Compression Logic

1. Probe codec — skip if already AV1 (`_av1`) or not matching mode
2. Detect HDR & bit depth → choose `p010` (10-bit) or `nv12` (8-bit) pixel format
3. Transcode with `av1_vaapi` at VBR 36M, quality preset 1
4. Preserve audio, subtitles, chapters, metadata, color info
5. Validate duration (±2% or ±2s) — discard corrupt output
6. If output ≥ 90% of original size → keep original, tag `_oth`
7. Restore original file timestamps on output

## Project Structure

```
FZY/
├── album_tui.py          # Core library + TUI + CLI (1259 lines)
├── webui.py              # HTTP server with SSE broadcasting (404 lines)
└── index.html            # Web UI frontend (677 lines)
```

Zero external Python packages — standard library only.

## License

Apache 2.0
