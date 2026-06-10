#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import queue
import sys
import threading
import re
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable

run_lock = threading.Lock()
cancel_event = threading.Event()
allowed_root: Path = Path()


class CancelledError(Exception):
    pass

def _validate_path(path_str: str, name: str, allowed: Path) -> tuple[Path | None, str | None]:
    try:
        resolved = Path(path_str).expanduser().resolve()
    except (OSError, RuntimeError) as e:
        return None, f"cannot resolve {name} '{path_str}': {e}"
    try:
        resolved.relative_to(allowed)
    except ValueError:
        return None, f"{name} '{resolved}' is outside allowed root '{allowed}'"
    return resolved, None


DEVICE_RE = re.compile(r"^/dev/dri/renderD\d+$")

def _validate_device(device: str) -> str | None:
    if not DEVICE_RE.match(device):
        return f"invalid device '{device}'; must match /dev/dri/renderD<N>"
    return None


HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import album_tui as tui

HOST = "127.0.0.1"
PORT = 8080

progress_state: dict = {"current": 0, "total": 0, "detail": "", "file_current": 0, "file_total": 0}
status_lock = threading.Lock()

sub_lock = threading.Lock()
sub_queues: list[queue.Queue] = []

def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=1000)
    with sub_lock:
        sub_queues.append(q)
    return q

def unsubscribe(q: queue.Queue) -> None:
    with sub_lock:
        try:
            sub_queues.remove(q)
        except ValueError:
            pass

def broadcast(msg: str) -> None:
    with sub_lock:
        dead: list[queue.Queue] = []
        for sq in sub_queues:
            try:
                sq.put_nowait(msg)
            except queue.Full:
                dead.append(sq)
        for sq in dead:
            sub_queues.remove(sq)

task_running = False
task_action = ""
task_result: str | None = None
task_state_lock = threading.Lock()
task_logs: list[str] = []
task_logs_lock = threading.Lock()
MAX_TASK_LOGS = 200


def clear_progress():
    with status_lock:
        progress_state.update(current=0, total=0, detail="", file_current=0, file_total=0)
    with task_state_lock:
        global task_running, task_result
        task_running = False
        task_result = None


def progress_cb(current: int, total: int, detail: str) -> None:
    if cancel_event.is_set():
        raise CancelledError("cancelled")
    completed = max(0, current - 1)
    with status_lock:
        progress_state.update(current=completed, total=total, detail=detail)
    broadcast(json.dumps(["progress", completed, total, detail]))


def file_progress_cb(current: int, total: int, file_current: int, file_total: int, detail: str) -> None:
    if cancel_event.is_set():
        raise CancelledError("cancelled")
    completed = max(0, current - 1)
    with status_lock:
        progress_state.update(current=completed, total=total, detail=detail, file_current=file_current, file_total=file_total)
    broadcast(json.dumps(["file_progress", completed, total, file_current, file_total, detail]))


class Logger:
    def __init__(self, log_file: Path | None = None):
        self.log_file = log_file
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            self.log_file.touch()

    def __call__(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with task_logs_lock:
            task_logs.append(line)
            if len(task_logs) > MAX_TASK_LOGS:
                del task_logs[:50]
        broadcast(json.dumps(["log", line]))
        if self.log_file:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

class Handler(BaseHTTPRequestHandler):
    def _check_host(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_port
        return host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")

    def do_GET(self):
        if not self._check_host():
            self._send_json(403, {"error": "forbidden"})
            return
        if self.path == "/":
            html_path = HERE / "index.html"
            try:
                html_content = html_path.read_text(encoding="utf-8")
                # 依然执行替换 __ROOT__ 的逻辑
                self._send_html(200, html_content.replace("__ROOT__", html.escape(str(allowed_root))))
            except FileNotFoundError:
                self._send_json(500, {"error": "index.html not found. Please ensure it is in the same directory."})
        elif self.path == "/api/events":
            self._send_sse()
        elif self.path == "/api/status":
            with status_lock, task_state_lock:
                self._send_json(200, {**progress_state, "running": task_running, "result": task_result})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_host():
            self._send_json(403, {"error": "forbidden"})
            return
        origin = self.headers.get("Origin")
        port = self.server.server_port
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}
        if origin and origin not in allowed:
            self._send_json(403, {"error": "Invalid Origin"})
            return
        if self.path == "/api/run":
            ct = self.headers.get("Content-Type", "").split(";")[0].strip()
            if ct != "application/json":
                self._send_json(415, {"error": "Unsupported Media Type"})
                return
            self._handle_run()
        elif self.path == "/api/cancel":
            self._handle_cancel()
        else:
            self._send_json(404, {"error": "not found"})

    def _send_html(self, code: int, html: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html.encode())))
        self.end_headers()
        self.wfile.write(html.encode())

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def _send_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        with status_lock, task_state_lock, task_logs_lock:
            sync_data = {
                "running": task_running,
                "action": task_action,
                "result": task_result,
                "progress": dict(progress_state),
                "logs": list(task_logs[-5:]) if task_logs else [],
            }
        try:
            self.wfile.write(f"data: {json.dumps(['sync', sync_data])}\n\n".encode())
        except BrokenPipeError:
            return

        my_queue = subscribe()
        try:
            while True:
                try:
                    msg = my_queue.get(timeout=3)
                except queue.Empty:
                    try:
                        self.wfile.write(b": heartbeat\n\n")
                    except BrokenPipeError:
                        return
                    continue
                try:
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except BrokenPipeError:
                    return
        finally:
            unsubscribe(my_queue)

    def _handle_run(self):
        if not run_lock.acquire(blocking=False):
            self._send_json(429, {"error": "operation already in progress"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > 1024 * 1024:
                run_lock.release()
                self._send_json(413, {"error": "Payload Too Large"})
                return
            raw = self.rfile.read(length).decode()
            data = json.loads(raw)
        except Exception as e:
            run_lock.release()
            self._send_json(400, {"error": str(e)})
            return

        action = data.get("action", "sort")
        dry_run = data.get("dry_run", False)

        device = data.get("device", "/dev/dri/renderD128")
        dev_err = _validate_device(device)
        if dev_err:
            run_lock.release()
            self._send_json(422, {"error": dev_err})
            return

        root, err = _validate_path(data.get("root", "."), "root", allowed_root)
        if err:
            run_lock.release()
            self._send_json(422, {"error": err})
            return

        compress_cfg = None
        pipeline_src = None
        pipeline_out = None
        if action in ("compress", "pipeline"):
            src_str = data.get("source_dir", str(root / tui.SORTED_DIRS["mp4"]))
            out_str = data.get("output_dir", src_str.rstrip('/') + '_C')
            src, err = _validate_path(src_str, "source_dir", allowed_root)
            if err:
                run_lock.release()
                self._send_json(422, {"error": err})
                return
            out, err = _validate_path(out_str, "output_dir", allowed_root)
            if err:
                run_lock.release()
                self._send_json(422, {"error": err})
                return
            if action == "compress":
                compress_cfg = tui.CompressionConfig(
                    source_dir=src,
                    output_dir=out,
                    keep_original=data.get("keep_original", True),
                    codec_mode=data.get("codec_mode", "non_av1"),
                )
            else:
                pipeline_src = src
                pipeline_out = out

        index_scan_root: Path | None = None
        index_file_path: Path | None = None
        if action in ("index", "pipeline"):
            if data.get("scan_root"):
                index_scan_root, err = _validate_path(data["scan_root"], "scan_root", allowed_root)
                if err:
                    run_lock.release()
                    self._send_json(422, {"error": err})
                    return
            if data.get("index_file"):
                index_file_path, err = _validate_path(data["index_file"], "index_file", allowed_root)
                if err:
                    run_lock.release()
                    self._send_json(422, {"error": err})
                    return

        self._send_json(200, {"status": "started"})
        cancel_event.clear()
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_file = root / f"flow_{action}_{ts}.log"
        logger = Logger(log_file=log_file)
        clear_progress()

        def wrapper():
            totals: dict[str, int] = {}
            with task_state_lock:
                global task_running, task_action, task_result
                task_running = True
                task_action = action
                task_result = None
            try:
                if action == "sort":
                    totals["sorted"] = tui.sort_by_extension(root, logger=logger, dry_run=dry_run, progress=progress_cb)
                elif action == "filter":
                    totals["quarantined"] = tui.filter_low_quality_videos(root, logger=logger, dry_run=dry_run, progress=progress_cb)
                elif action == "index":
                    totals["indexed"] = tui.sync_blake3_index(root, logger=logger, dry_run=dry_run, progress=progress_cb, scan_root=index_scan_root, index_path=index_file_path)
                elif action == "compress" and compress_cfg is not None:
                    totals["compressed"] = tui.compress_all_mp4(
                        compress_cfg,
                        device=device,
                        logger=logger, dry_run=dry_run, progress=progress_cb, file_progress=file_progress_cb,
                    )
                elif action == "pipeline":
                    tui.full_pipeline(root, device=device,
                                      logger=logger, dry_run=dry_run, progress=progress_cb,
                                      file_progress=file_progress_cb,
                                      keep_original=data.get("keep_original", True),
                                      codec_mode=data.get("codec_mode", "non_av1"),
                                      compress_source_dir=pipeline_src,
                                      compress_output_dir=pipeline_out,
                                      index_scan_root=index_scan_root,
                                      index_path=index_file_path)
                logger(f"=== {action} done | summary: {json.dumps(totals) if totals else 'see steps above'} | log: {log_file} ===")
                broadcast(json.dumps(["done"]))
                with task_state_lock:
                    task_result = "done"
            except CancelledError:
                broadcast(json.dumps(["cancelled"]))
                with task_state_lock:
                    task_result = "cancelled"
            except Exception as e:
                broadcast(json.dumps(["error", str(e)]))
                with task_state_lock:
                    task_result = "error"
            finally:
                with task_state_lock:
                    task_running = False
                run_lock.release()

        threading.Thread(target=wrapper, daemon=True).start()

    def _handle_cancel(self):
        with task_state_lock:
            if not task_running:
                self._send_json(400, {"error": "no task running"})
                return
        cancel_event.set()
        self._send_json(200, {"status": "cancelling"})


def main():
    parser = argparse.ArgumentParser(description="Flow Web UI")
    parser.add_argument("--port", type=int, default=PORT, help=f"port (default: {PORT})")
    parser.add_argument("--root", default=".", help="default root directory")
    args = parser.parse_args()

    global allowed_root
    allowed_root = Path(args.root).expanduser().resolve()

    server = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"Flow Web UI at http://{HOST}:{args.port}")
    print(f"Default root: {Path(args.root).resolve()}")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
