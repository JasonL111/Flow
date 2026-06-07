#!/usr/bin/env python3
from __future__ import annotations

import argparse
import curses
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".lrf", ".ts", ".mts"}
SORTED_DIRS = {"mp4": "MP4", "lrf": "LRF", "other": "OTHER", "low": "LOW_QUALITY"}
INDEX_NAME = ".blake3"
SCRIPT_NAME = Path(__file__).name
SIZE_KEEP_RATIO = 0.95
COMPRESSED_TAG = ".av1"


def compressed_companion_path(source: Path, output_dir: Path) -> Path:
    return output_dir / f"{source.stem}{COMPRESSED_TAG}{source.suffix}"


def is_compressed_output(file_path: Path) -> bool:
    return file_path.stem.endswith(COMPRESSED_TAG)


@dataclass
class IndexEntry:
    path: str
    blake3: str
    size: int
    mtime_ns: int


@dataclass
class CompressionConfig:
    source_dir: Path
    output_dir: Path
    keep_original: bool
    codec_mode: str


@dataclass
class ProgressState:
    current: int = 0
    total: int = 0
    detail: str = ""
    file_current: int = 0
    file_total: int = 0


ProgressCallback = Callable[[int, int, str], None]
FileProgressCallback = Callable[[int, int, int, int, str], None]


class ConsoleProgress:
    def __init__(self) -> None:
        self.active = False

    def __call__(self, current: int, total: int, detail: str) -> None:
        self.active = True
        sys.stderr.write("\r" + format_progress(current, total, detail, width=28).ljust(96))
        sys.stderr.flush()

    def finish(self) -> None:
        if self.active:
            sys.stderr.write("\n")
            sys.stderr.flush()
            self.active = False


def is_managed_path(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = rel.parts
    return bool(parts) and parts[0] in SORTED_DIRS.values()


def iter_files(
    root: Path,
    include_managed: bool = False,
    skip_dirs: Iterable[str] | None = None,
) -> Iterable[Path]:
    skip = set(skip_dirs or ())
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name == INDEX_NAME:
            continue
        if path.name == SCRIPT_NAME:
            continue
        if not include_managed and is_managed_path(path, root):
            continue
        if skip:
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if any(part in skip for part in rel.parts[:-1]):
                continue
        yield path


def unique_destination(dest_dir: Path, src: Path, create_dir: bool = True) -> Path:
    if create_dir:
        dest_dir.mkdir(parents=True, exist_ok=True)
    candidate = dest_dir / src.name
    if not candidate.exists():
        return candidate
    stem = src.stem
    suffix = src.suffix
    i = 1
    while True:
        candidate = dest_dir / f"{stem}__{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _find_duplicate(dst_dir: Path, src: Path) -> Path | None:
    """Find a file in dst_dir whose blake3 hash matches src. Size is checked first
    to short-circuit the vast majority of mismatches without hashing."""
    try:
        src_size = src.stat().st_size
    except OSError:
        return None
    src_hash: str | None = None
    for candidate in dst_dir.rglob("*"):
        if not candidate.is_file():
            continue
        if candidate.name in {INDEX_NAME, SCRIPT_NAME}:
            continue
        try:
            if candidate.stat().st_size != src_size:
                continue
        except OSError:
            continue
        if src_hash is None:
            src_hash = hash_with_b3sum(src)
        try:
            if hash_with_b3sum(candidate) == src_hash:
                return candidate
        except RuntimeError:
            continue
    return None


def move_file(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    exact = dst_dir / src.name
    if not exact.exists():
        shutil.move(str(src), str(exact))
        return exact
    dup = _find_duplicate(dst_dir, src)
    if dup is not None:
        src.unlink()
        return dup
    final = unique_destination(dst_dir, src)
    shutil.move(str(src), str(final))
    return final


def format_progress(
    current: int,
    total: int,
    detail: str = "",
    width: int = 24,
    file_current: int = 0,
    file_total: int = 0,
) -> str:
    total = max(total, 0)
    if total <= 0:
        bar = "-" * width
    else:
        filled = max(0, min(width, int(width * current / total)))
        bar = "#" * filled + "-" * (width - filled)
    suffix = f" {detail}" if detail else ""
    if file_total > 0:
        pct = max(0, min(100, int(100 * file_current / file_total)))
        suffix = f"{suffix} [{pct}%]" if suffix else f" [{pct}%]"
    return f"[{bar}] {current}/{total}{suffix}"


def emit_progress(progress: ProgressCallback | None, current: int, total: int, detail: str = "") -> None:
    if progress is not None:
        progress(current, total, detail)


def ffprobe_duration(file_path: Path) -> float | None:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def ffprobe_video_codec(file_path: Path) -> str | None:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(file_path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    codec = proc.stdout.strip().lower()
    return codec or None


HDR_TRANSFERS = {
    "smpte2084",
    "arib-std-b67",
    "smpte428",
    "smpte431",
    "smpte432",
    "iec61966-2-1",
}


@dataclass
class VideoInfo:
    duration: float | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    pix_fmt: str | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None
    color_range: str | None = None
    stream_count: int = 0
    has_data_streams: bool = False

    @property
    def is_10bit(self) -> bool:
        pix = (self.pix_fmt or "").lower()
        return pix.endswith("10le") or pix.endswith("10be") or pix.endswith("p10") or "10le" in pix

    @property
    def is_hdr(self) -> bool:
        if (self.color_transfer or "").lower() in HDR_TRANSFERS:
            return True
        if self.is_10bit and (self.color_primaries or "").lower() == "bt2020":
            return True
        return False


def ffprobe_video_info(file_path: Path) -> VideoInfo:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(file_path),
        ],
        capture_output=True,
        text=True,
    )
    info = VideoInfo()
    if proc.returncode != 0:
        return info
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return info
    fmt = data.get("format") or {}
    try:
        info.duration = float(fmt.get("duration")) if fmt.get("duration") else None
    except (TypeError, ValueError):
        info.duration = None
    streams = data.get("streams") or []
    info.stream_count = len(streams)
    info.has_data_streams = any((s.get("codec_type") == "data") for s in streams)
    for stream in streams:
        if stream.get("codec_type") == "video" and info.video_codec is None:
            info.video_codec = (stream.get("codec_name") or "").lower() or None
            info.width = stream.get("width")
            info.height = stream.get("height")
            info.pix_fmt = stream.get("pix_fmt")
            info.color_primaries = stream.get("color_primaries")
            info.color_transfer = stream.get("color_transfer")
            info.color_space = stream.get("color_space")
            info.color_range = stream.get("color_range")
    return info


def _safe_color_value(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.lower()
    if lowered in {"unknown", "unspecified", "reserved", ""}:
        return None
    return value


def _build_compress_cmd(
    file_path: Path,
    device: str,
    info: VideoInfo,
    output_path: Path,
) -> list[str]:
    fmt_filter = "format=p010,hwupload" if info.is_10bit else "format=nv12,hwupload"
    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-vaapi_device",
        device,
        "-i",
        str(file_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map",
        "0:t?",
        "-map",
        "0:s?",
        "-vf",
        fmt_filter,
        "-c:v",
        "av1_vaapi",
        "-rc_mode",
        "VBR",
        "-b:v",
        "36M",
        "-maxrate",
        "46M",
        "-g",
        "240",
        "-quality",
        "1",
        "-c:a",
        "copy",
        "-c:s",
        "copy",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-movflags",
        "use_metadata_tags",
    ]
    for flag, value in (
        ("color_primaries", info.color_primaries),
        ("color_trc", info.color_transfer),
        ("colorspace", info.color_space),
        ("color_range", info.color_range),
    ):
        safe = _safe_color_value(value)
        if safe:
            cmd += [f"-{flag}", safe]
    cmd.append(str(output_path))
    return cmd


def _ffmpeg_total_us(duration: float | None) -> int | None:
    if duration is None or duration <= 0:
        return None
    return int(duration * 1_000_000)


def _verify_duration(
    src_duration: float | None,
    dst: Path,
    tolerance_ratio: float = 0.02,
    tolerance_seconds: float = 2.0,
) -> tuple[bool, float | None]:
    """Catch VAAPI soft-corruption: ffmpeg returns 0 and the file is non-empty but
    truncated. Compare output duration to source; allow either a 2% ratio or 2s
    absolute drift, whichever is larger."""
    if src_duration is None or src_duration <= 0:
        return True, None
    dst_duration = ffprobe_duration(dst)
    if dst_duration is None or dst_duration <= 0:
        return False, None
    drift = abs(src_duration - dst_duration)
    allowed = max(tolerance_seconds, src_duration * tolerance_ratio)
    return drift <= allowed, dst_duration


def should_compress_codec(codec: str | None, codec_mode: str) -> tuple[bool, str]:
    if codec is None:
        return False, "unknown codec"
    if codec_mode == "hevc_only":
        if codec in {"hevc", "h265"}:
            return True, ""
        if codec == "av1":
            return False, "already av1"
        return False, f"skip codec={codec}"
    if codec_mode == "non_av1":
        if codec == "av1":
            return False, "already av1"
        return True, ""
    return False, f"unknown codec mode={codec_mode}"


def sort_by_extension(root: Path, logger=print, dry_run: bool = False, progress: ProgressCallback | None = None) -> int:
    files = list(iter_files(root, include_managed=False))
    moved = 0
    total = len(files)
    for idx, file_path in enumerate(files, start=1):
        ext = file_path.suffix.lower()
        if ext == ".mp4":
            dst_dir = root / SORTED_DIRS["mp4"]
        elif ext == ".lrf":
            dst_dir = root / SORTED_DIRS["lrf"]
        else:
            dst_dir = root / SORTED_DIRS["other"]
        new_path = unique_destination(dst_dir, file_path, create_dir=not dry_run)
        if dry_run:
            logger(f"would move: {file_path} -> {new_path}")
        else:
            new_path = move_file(file_path, dst_dir)
            logger(f"moved: {file_path} -> {new_path}")
        moved += 1
        emit_progress(progress, idx, total, file_path.name)
    return moved


def filter_low_quality_videos(
    root: Path,
    logger=print,
    threshold: float = 3.0,
    dry_run: bool = False,
    progress: ProgressCallback | None = None,
) -> int:
    files = list(
        iter_files(
            root,
            include_managed=True,
            skip_dirs={SORTED_DIRS["low"]},
        )
    )
    quarantined = 0
    dst_dir = root / SORTED_DIRS["low"]
    total = len(files)
    for idx, file_path in enumerate(files, start=1):
        if file_path.suffix.lower() not in VIDEO_EXTS:
            emit_progress(progress, idx, total, file_path.name)
            continue
        duration = ffprobe_duration(file_path)
        if duration is None or duration <= threshold:
            new_path = unique_destination(dst_dir, file_path, create_dir=not dry_run)
            reason = "corrupt" if duration is None else f"duration={duration:.3f}s"
            if dry_run:
                logger(f"would quarantine: {file_path} -> {new_path} ({reason})")
            else:
                new_path = move_file(file_path, dst_dir)
                logger(f"quarantined: {file_path} -> {new_path} ({reason})")
            quarantined += 1
        emit_progress(progress, idx, total, file_path.name)
    return quarantined


def compress_mp4_with_vaapi(
    file_path: Path,
    device: str,
    logger=print,
    dry_run: bool = False,
    output_dir: Path | None = None,
    keep_original: bool = False,
    file_index: int = 0,
    file_total: int = 0,
    file_progress: FileProgressCallback | None = None,
) -> bool:
    output_dir = output_dir or file_path.parent
    if dry_run:
        if keep_original:
            final_path = compressed_companion_path(file_path, output_dir)
        elif output_dir != file_path.parent:
            final_path = unique_destination(output_dir, file_path, create_dir=False)
        else:
            final_path = file_path
        logger(f"would compress: {file_path} -> {final_path}")
        return True

    info = ffprobe_video_info(file_path)
    try:
        original_stat = file_path.stat()
    except OSError as exc:
        logger(f"failed: {file_path} ({exc})")
        return False
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_path = output_dir / f"{file_path.stem}_temp_av1{file_path.suffix}"
    cmd = _build_compress_cmd(file_path, device, info, temp_path)

    total_us = _ffmpeg_total_us(info.duration) or 0
    if file_progress is not None and total_us > 0:
        file_progress(file_index, file_total, 0, total_us, file_path.name)

    stderr_lines: list[str] = []
    last_progress_ts = [0]

    def _emit(ts_us: int) -> None:
        if file_progress is None or total_us <= 0:
            return
        if ts_us - last_progress_ts[0] < max(total_us // 200, 50_000):
            return
        last_progress_ts[0] = ts_us
        file_progress(file_index, file_total, ts_us, total_us, file_path.name)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        logger(f"failed: {file_path} ({exc})")
        return False

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("out_time_us="):
            try:
                ts = int(line.split("=", 1)[1])
            except ValueError:
                continue
            _emit(ts)
        elif line == "progress=end":
            _emit(total_us)
    assert proc.stderr is not None
    stderr_text = proc.stderr.read()
    proc.wait()
    if stderr_text:
        stderr_lines = [ln for ln in stderr_text.splitlines() if ln.strip()]

    if file_progress is not None and total_us > 0:
        file_progress(file_index, file_total, total_us, total_us, file_path.name)

    if proc.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0:
        ok, dst_dur = _verify_duration(info.duration, temp_path)
        if not ok:
            if temp_path.exists():
                temp_path.unlink()
            logger(
                f"failed: {file_path} (duration mismatch: src={info.duration:.3f}s out={dst_dur!r})"
            )
            return False
        new_size = temp_path.stat().st_size
        original_size = original_stat.st_size
        if new_size >= original_size * SIZE_KEEP_RATIO:
            temp_path.unlink()
            logger(
                f"skipped: {file_path} (output not smaller: {new_size}B >= {original_size}B * {SIZE_KEEP_RATIO})"
            )
            return False
        if keep_original:
            final_path = compressed_companion_path(file_path, output_dir)
            if final_path.exists():
                temp_path.unlink()
                logger(f"skipped: {file_path} (companion exists: {final_path.name})")
                return False
            os.replace(temp_path, final_path)
        elif output_dir != file_path.parent:
            final_path = unique_destination(output_dir, file_path)
            os.replace(temp_path, final_path)
            if file_path.exists():
                file_path.unlink()
        else:
            final_path = file_path
            os.replace(temp_path, file_path)
        try:
            os.utime(
                final_path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
        except OSError as exc:
            logger(f"warning: could not restore mtime on {final_path} ({exc})")
        logger(f"compressed: {file_path} -> {final_path} ({original_size}B -> {new_size}B)")
        return True
    if temp_path.exists():
        temp_path.unlink()
    logger(f"failed: {file_path}")
    if stderr_lines:
        logger(stderr_lines[-1])
    return False


def compress_all_mp4(
    config: CompressionConfig,
    device: str,
    logger=print,
    dry_run: bool = False,
    progress: ProgressCallback | None = None,
    file_progress: FileProgressCallback | None = None,
) -> int:
    if not config.source_dir.exists():
        logger(f"missing folder: {config.source_dir}")
        return 0
    files = [
        file_path
        for file_path in sorted(config.source_dir.rglob("*"))
        if file_path.is_file()
        and file_path.suffix.lower() == ".mp4"
        and not file_path.name.lower().endswith("_temp_av1.mp4")
        and not is_compressed_output(file_path)
    ]
    count = 0
    total = len(files)
    for idx, file_path in enumerate(files, start=1):
        codec = ffprobe_video_codec(file_path)
        should_run, reason = should_compress_codec(codec, config.codec_mode)
        if not should_run:
            logger(f"skipped: {file_path} ({reason or 'no match'})")
            emit_progress(progress, idx, total, file_path.name)
            continue
        if config.keep_original:
            companion = compressed_companion_path(file_path, config.output_dir)
            if companion.exists():
                logger(f"skipped: {file_path} (companion exists: {companion.name})")
                emit_progress(progress, idx, total, file_path.name)
                continue
        if compress_mp4_with_vaapi(
            file_path,
            device=device,
            logger=logger,
            dry_run=dry_run,
            output_dir=config.output_dir,
            keep_original=config.keep_original,
            file_index=idx,
            file_total=total,
            file_progress=file_progress,
        ):
            count += 1
        emit_progress(progress, idx, total, file_path.name)
    return count


def hash_with_b3sum(file_path: Path) -> str:
    proc = subprocess.run(["b3sum", "--no-names", str(file_path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"b3sum failed for {file_path}")
    return proc.stdout.strip()


def load_index(index_path: Path) -> dict[str, IndexEntry]:
    entries: dict[str, IndexEntry] = {}
    if not index_path.exists():
        return entries
    for raw in index_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t", 3)
        if len(parts) != 4:
            continue
        blake3, size_s, mtime_s, rel = parts
        try:
            entries[rel] = IndexEntry(path=rel, blake3=blake3, size=int(size_s), mtime_ns=int(mtime_s))
        except ValueError:
            continue
    return entries


def write_index(index_path: Path, entries: dict[str, IndexEntry]) -> None:
    lines = [f"{e.blake3}\t{e.size}\t{e.mtime_ns}\t{e.path}" for e in sorted(entries.values(), key=lambda x: x.path)]
    index_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def sync_blake3_index(
    root: Path,
    logger=print,
    dry_run: bool = False,
    progress: ProgressCallback | None = None,
    index_path: Path | None = None,
) -> int:
    index_path = index_path or (root / INDEX_NAME)
    if not dry_run and not index_path.exists():
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.touch()
    existing = load_index(index_path)
    current: dict[str, IndexEntry] = {}
    files = list(iter_files(root, include_managed=True))
    total = len(files)
    for idx, file_path in enumerate(files, start=1):
        rel = file_path.relative_to(root).as_posix()
        stat = file_path.stat()
        cached = existing.get(rel)
        if cached and cached.size == stat.st_size and cached.mtime_ns == stat.st_mtime_ns:
            current[rel] = cached
            emit_progress(progress, idx, total, rel)
            continue
        digest = hash_with_b3sum(file_path)
        current[rel] = IndexEntry(path=rel, blake3=digest, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        logger(f"{'would index' if dry_run else 'indexed'}: {rel}")
        emit_progress(progress, idx, total, rel)
    if not dry_run:
        write_index(index_path, current)
    return len(current)


def full_pipeline(
    root: Path,
    device: str,
    logger=print,
    dry_run: bool = False,
    progress: ProgressCallback | None = None,
    keep_original: bool = True,
) -> None:
    logger("step 1: filter low-quality videos")
    filter_low_quality_videos(root, logger=logger, dry_run=dry_run, progress=progress)
    logger("step 2: sort by extension")
    sort_by_extension(root, logger=logger, dry_run=dry_run, progress=progress)
    logger(f"step 3: compress mp4 (keep_original={keep_original})")
    compress_all_mp4(
        CompressionConfig(
            source_dir=root / SORTED_DIRS["mp4"],
            output_dir=root / SORTED_DIRS["mp4"],
            keep_original=keep_original,
            codec_mode="non_av1",
        ),
        device=device,
        logger=logger,
        dry_run=dry_run,
        progress=progress,
    )
    logger("step 4: sync blake3 index")
    sync_blake3_index(root, logger=logger, dry_run=dry_run, progress=progress)


class TuiApp:
    def __init__(self, root: Path, device: str, dry_run: bool = False):
        self.root = root
        self.device = device
        self.dry_run = dry_run
        self.status: List[str] = []
        self.progress: ProgressState | None = None
        self.items = [
            "Filter low-quality videos",
            "Sort by extension",
            "Compress MP4 with AMD VAAPI",
            "Sync BLAKE3 index",
            "Run full pipeline",
            "Change root directory",
            "Quit",
        ]
        self.selected = 0
        self.quit_requested = False

    def log(self, message: str) -> None:
        self.status.append(message)
        self.status = self.status[-8:]

    def prompt(self, stdscr, title: str, default: str = "") -> str:
        buf = list(default)
        pos = len(buf)
        help_text = "Enter: accept  Esc/Ctrl-C: cancel"
        try:
            prev_cursor = curses.curs_set(1)
        except curses.error:
            prev_cursor = None
        try:
            while True:
                stdscr.clear()
                stdscr.addstr(0, 0, title)
                stdscr.addstr(1, 0, help_text)
                stdscr.addstr(3, 0, "".join(buf))
                stdscr.move(3, pos)
                stdscr.refresh()
                try:
                    key = stdscr.get_wch()
                except KeyboardInterrupt:
                    return default
                except curses.error:
                    continue

                if key in ("\n", "\r"):
                    value = "".join(buf).strip()
                    return value or default
                if key in ("\x1b", "\x03"):
                    return default
                if key in (curses.KEY_LEFT,):
                    pos = max(0, pos - 1)
                elif key in (curses.KEY_RIGHT,):
                    pos = min(len(buf), pos + 1)
                elif key in (curses.KEY_HOME,):
                    pos = 0
                elif key in (curses.KEY_END,):
                    pos = len(buf)
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    if pos > 0:
                        del buf[pos - 1]
                        pos -= 1
                elif key in (curses.KEY_DC,):
                    if pos < len(buf):
                        del buf[pos]
                elif isinstance(key, str) and key.isprintable():
                    buf.insert(pos, key)
                    pos += 1
        finally:
            if prev_cursor is not None:
                try:
                    curses.curs_set(prev_cursor)
                except curses.error:
                    pass

    def prompt_yes_no(self, stdscr, title: str, default: bool = False) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        answer = self.prompt(stdscr, f"{title} {suffix}", "y" if default else "n")
        return answer.lower() in {"y", "yes", "1", "true"}

    def prompt_choice(self, stdscr, title: str, options: list[str], default_index: int = 0) -> str:
        lines = [f"{i + 1}. {label}" for i, label in enumerate(options)]
        choice = self.prompt(
            stdscr,
            f"{title} {' | '.join(lines)}",
            str(default_index + 1),
        )
        try:
            idx = int(choice) - 1
        except ValueError:
            idx = default_index
        idx = max(0, min(len(options) - 1, idx))
        return options[idx]

    def draw(self, stdscr) -> None:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        mode = "dry-run" if self.dry_run else "live"
        stdscr.addstr(0, 0, f"Album TUI  root={self.root}  device={self.device}  mode={mode}")
        stdscr.addstr(1, 0, "Up/Down to move, Enter to run, r to change root, q to quit")
        item_start = 3
        if self.progress is not None:
            p = self.progress
            bar = format_progress(
                p.current,
                p.total,
                p.detail,
                width=max(8, w - 22),
                file_current=p.file_current,
                file_total=p.file_total,
            )
            stdscr.addstr(2, 0, f"Progress: {bar[: max(0, w - 10)]}")
            item_start = 4
        for i, item in enumerate(self.items):
            marker = ">" if i == self.selected else " "
            stdscr.addstr(item_start + i, 0, f"{marker} {item}")
        start = max(0, h - len(self.status) - 2)
        stdscr.addstr(start, 0, "Logs:")
        for idx, line in enumerate(self.status[-(h - start - 1):], start + 1):
            stdscr.addstr(idx, 0, line[: max(0, w - 1)])
        stdscr.refresh()

    def progress_cb(self, stdscr) -> ProgressCallback:
        def _update(current: int, total: int, detail: str) -> None:
            self.progress = ProgressState(current=current, total=total, detail=detail)
            self.draw(stdscr)

        return _update

    def file_progress_cb(self, stdscr) -> FileProgressCallback:
        def _update(
            current: int,
            total: int,
            file_current: int,
            file_total: int,
            detail: str,
        ) -> None:
            prev = self.progress
            if prev is not None:
                main_current = prev.current
                main_total = prev.total
            else:
                main_current = 0
                main_total = total
            self.progress = ProgressState(
                current=main_current,
                total=main_total,
                detail=detail,
                file_current=file_current,
                file_total=file_total,
            )
            self.draw(stdscr)

        return _update

    def run_action(self, stdscr, action: str) -> None:
        if action == "Quit":
            self.quit_requested = True
            return
        try:
            progress = self.progress_cb(stdscr)
            if action == "Filter low-quality videos":
                n = filter_low_quality_videos(self.root, logger=self.log, dry_run=self.dry_run, progress=progress)
                self.log(f"done: {n} quarantined")
            elif action == "Sort by extension":
                n = sort_by_extension(self.root, logger=self.log, dry_run=self.dry_run, progress=progress)
                self.log(f"done: {n} moved")
            elif action == "Compress MP4 with AMD VAAPI":
                source_dir = Path(self.prompt(stdscr, "Enter source directory for compression:", str(self.root / SORTED_DIRS["mp4"]))).expanduser().resolve()
                output_dir = Path(self.prompt(stdscr, "Enter output directory for compressed files:", str(source_dir))).expanduser().resolve()
                keep_original = self.prompt_yes_no(stdscr, "Keep original files after compression?", default=True)
                codec_label = self.prompt_choice(
                    stdscr,
                    "Choose compression standard:",
                    ["hevc_only (only H265/HEVC -> AV1)", "non_av1 (compress all MP4 videos)"],
                    default_index=1,
                )
                codec_mode = "hevc_only" if codec_label.startswith("hevc_only") else "non_av1"
                n = compress_all_mp4(
                    CompressionConfig(
                        source_dir=source_dir,
                        output_dir=output_dir,
                        keep_original=keep_original,
                        codec_mode=codec_mode,
                    ),
                    device=self.device,
                    logger=self.log,
                    dry_run=self.dry_run,
                    progress=progress,
                    file_progress=self.file_progress_cb(stdscr),
                )
                self.log(f"done: {n} compressed")
            elif action == "Sync BLAKE3 index":
                index_path = Path(self.prompt(stdscr, "Enter BLAKE3 index file path:", str(self.root / INDEX_NAME))).expanduser().resolve()
                n = sync_blake3_index(self.root, logger=self.log, dry_run=self.dry_run, progress=progress, index_path=index_path)
                self.log(f"done: {n} indexed")
            elif action == "Run full pipeline":
                keep_original = self.prompt_yes_no(
                    stdscr,
                    "Keep original files after compression?",
                    default=True,
                )
                full_pipeline(
                    self.root,
                    self.device,
                    logger=self.log,
                    dry_run=self.dry_run,
                    progress=progress,
                    keep_original=keep_original,
                )
                self.log("done: pipeline finished")
            elif action == "Change root directory":
                new_root = self.prompt(stdscr, "Enter new root directory:", str(self.root))
                self.root = Path(new_root).expanduser().resolve()
                self.log(f"root changed to {self.root}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"error: {exc}")
        finally:
            self.progress = None

    def main(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.nodelay(False)
        while not self.quit_requested:
            self.draw(stdscr)
            key = stdscr.getch()
            if key in (ord("q"), 27):
                break
            if key in (curses.KEY_UP, ord("k")):
                self.selected = (self.selected - 1) % len(self.items)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.selected = (self.selected + 1) % len(self.items)
            elif key in (curses.KEY_ENTER, 10, 13):
                self.run_action(stdscr, self.items[self.selected])
            elif key == ord("r"):
                self.selected = self.items.index("Change root directory")
                self.run_action(stdscr, self.items[self.selected])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TUI photo/video organizer")
    parser.add_argument("--path", "--root", dest="root", default=".", help="target directory")
    parser.add_argument("--device", default="/dev/dri/renderD128", help="VAAPI device path")
    parser.add_argument("--dry-run", action="store_true", help="preview changes without modifying files")
    parser.add_argument("--index-file", default=None, help="BLAKE3 index file path")
    parser.add_argument("--compress-source", default=None, help="compression source directory")
    parser.add_argument("--compress-output", default=None, help="compression output directory")
    parser.add_argument("--keep-original", action="store_true", help="keep original files after compression")
    parser.add_argument("--codec-mode", choices=("hevc_only", "non_av1"), default="non_av1", help="compression selection mode")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("tui", help="interactive terminal UI")
    sub.add_parser("sort", help="sort by extension")
    sub.add_parser("filter", help="filter low-quality videos")
    sub.add_parser("compress", help="compress MP4 files")
    sub.add_parser("index", help="sync BLAKE3 index")
    sub.add_parser("all", help="run full pipeline")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"root does not exist: {root}", file=sys.stderr)
        return 2

    if args.cmd in (None, "tui"):
        try:
            curses.wrapper(lambda stdscr: TuiApp(root, args.device, args.dry_run).main(stdscr))
        except KeyboardInterrupt:
            return 130
        return 0
    progress = ConsoleProgress()
    try:
        if args.cmd == "sort":
            sort_by_extension(root, dry_run=args.dry_run, progress=progress)
        elif args.cmd == "filter":
            filter_low_quality_videos(root, dry_run=args.dry_run, progress=progress)
        elif args.cmd == "compress":
            source_dir = Path(args.compress_source).expanduser().resolve() if args.compress_source else root / SORTED_DIRS["mp4"]
            output_dir = Path(args.compress_output).expanduser().resolve() if args.compress_output else source_dir
            compress_all_mp4(
                CompressionConfig(
                    source_dir=source_dir,
                    output_dir=output_dir,
                    keep_original=args.keep_original,
                    codec_mode=args.codec_mode,
                ),
                args.device,
                dry_run=args.dry_run,
                progress=progress,
            )
        elif args.cmd == "index":
            index_path = Path(args.index_file).expanduser().resolve() if args.index_file else None
            sync_blake3_index(root, dry_run=args.dry_run, progress=progress, index_path=index_path)
        elif args.cmd == "all":
            full_pipeline(
                root,
                args.device,
                dry_run=args.dry_run,
                progress=progress,
                keep_original=args.keep_original,
            )
    finally:
        progress.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
