"""Bounded, asynchronous, privacy-filtered snapshots of booking HTTP traffic."""

import atexit
import contextvars
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx


_LOG = logging.getLogger(__name__)
_CONTEXT = contextvars.ContextVar("http_capture_context", default={})
_RECORDER = None
_RECORDER_LOCK = threading.Lock()
_REDACTED = "[REDACTED]"
_SAFE_QUERY = {"id", "type", "s_date", "s_dates", "serviceid", "stockid", "json", "_"}
_SAFE_HEADERS = {"date", "age", "content-type", "content-length", "server", "via",
                 "retry-after", "x-cache", "x-request-id", "cache-control", "host",
                 "user-agent", "accept", "accept-encoding", "x-requested-with", "content-encoding"}
_SENSITIVE = re.compile(
    r"password|passwd|secret|token|cookie|session|authorization|execution|ticket|"
    r"username|loginname|realname|phone|mobile|email|idcard|idnumber|student|userid|"
    r"account|openid|unionid|contact|person|customer|member|operator", re.I)
_JSON_PATHS = {"/product/findtime.html", "/product/findOkArea.html",
               "/order/book.html", "/order/tobook.html"}
_CAPTURE_PATHS = _JSON_PATHS | {"/seat/seat.html", "/order/show.html", "/gen"}


@contextmanager
def capture_context(**values):
    token = _CONTEXT.set({"trace_id": uuid.uuid4().hex, **_CONTEXT.get(), **values})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def submit_with_capture_context(executor, function, *args):
    return executor.submit(contextvars.copy_context().run, function, *args)


def _enabled():
    return os.getenv("BOOKING_HTTP_CAPTURE", "1").strip().lower() not in {"0", "false", "off", "no"}


def _integer_setting(name, default, minimum):
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _path(url):
    path = urlsplit(str(url)).path
    return path[4:] if path.startswith("/web/") else path


def _safe_url(url):
    parts = urlsplit(str(url))
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    query = [(key, value if key in _SAFE_QUERY else _REDACTED)
             for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, host, parts.path, urlencode(query), ""))


def _safe_headers(headers):
    result = {key.lower(): value for key, value in headers.items() if key.lower() in _SAFE_HEADERS}
    if "location" in headers:
        result["location"] = _safe_url(headers["location"])
    return result


def _scrub(value):
    if isinstance(value, dict):
        return {key: _REDACTED if _SENSITIVE.search(key) or key.lower() in
                {"yzm", "captcha", "code", "name", "user", "users", "credentials", "xh", "xm", "gh", "sfzh", "sno", "tel"}
                else _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _body_snapshot(content, complete_size, path, request=False):
    result = {"bytes": complete_size, "truncated": complete_size is not None and complete_size > len(content)}
    if complete_size is None:
        return {**result, "omitted": "unread_stream"}
    if result["truncated"]:
        return {**result, "omitted": "body_limit"}
    if not content:
        return {**result, "text": "", "format": "empty"}
    text = content.decode("utf-8", errors="replace")
    if request and path in {"/order/show.html", "/order/book.html", "/order/tobook.html"}:
        fields = []
        for key, value in parse_qsl(text, keep_blank_values=True):
            if key == "param":
                try:
                    value = json.dumps(_scrub(json.loads(value)), ensure_ascii=False)
                except (ValueError, TypeError):
                    value = _REDACTED
            elif key != "json":
                value = _REDACTED
            fields.append((key, value))
        return {**result, "format": "redacted_form", "text": urlencode(fields)}
    if not request and path in _JSON_PATHS:
        try:
            original = json.loads(text)
            scrubbed = _scrub(original)
            return {**result, "format": "json", "redacted": original != scrubbed,
                    "text": text if original == scrubbed else json.dumps(scrubbed, ensure_ascii=False)}
        except (ValueError, TypeError):
            pass
    if not request and path == "/seat/seat.html":
        fields = re.findall(r'<input\b[^>]*\bid=["\']txt_seatid["\'][^>]*>', text, re.I)
        if fields:
            values = []
            for field in fields:
                match = re.search(r'\bvalue=["\']([0-9_,]*)["\']', field, re.I)
                if match:
                    values.append(match.group(1))
            if values:
                return {**result, "format": "seat_field", "txt_seatid": values}
    signals = re.findall(r"\u8bf7\u5230\s*\d{1,2}:\d{2}\s*[-~\u81f3]\s*\d{1,2}:\d{2}\s*\u518d\u6765\u9884\u8ba2[\uff01!]?", text)
    return {**result, "omitted": "privacy_policy", "time_gate_messages": signals}


def _snapshot_message(message, body_limit):
    try:
        content = message.content
        size = len(content)
    except (httpx.RequestNotRead, httpx.ResponseNotRead):
        content, size = b"", None
    return {"headers": dict(message.headers), "content": content[:body_limit], "size": size}


def _serialize(snapshot):
    request = snapshot["request"]
    path = _path(request["url"])
    result = {key: value for key, value in snapshot.items() if key not in {"request", "response", "redirects"}}
    result["request"] = {
        "method": request["method"], "url": _safe_url(request["url"]),
        "headers": _safe_headers(request["headers"]),
        "body": _body_snapshot(request["content"], request["size"], path, request=True),
    }
    response = snapshot.get("response")
    if response is not None:
        result["response"] = {
            "status_code": response["status_code"], "headers": _safe_headers(response["headers"]),
            "url": _safe_url(response["url"]),
            "body": _body_snapshot(response["content"], response["size"], _path(response["url"])),
        }
    result["redirects"] = [
        {"url": _safe_url(hop["url"]), "method": hop["method"],
         "status_code": hop["status_code"], "headers": _safe_headers(hop["headers"])}
        for hop in snapshot.get("redirects", [])
    ]
    return result


class HttpCapture:
    def __init__(self, directory, *, body_limit=262144, queue_size=256,
                 file_bytes=8 * 1024 * 1024, total_bytes=10 * 1024 * 1024 * 1024, retention_days=30):
        self.directory = Path(directory)
        self.body_limit = body_limit
        self.file_bytes = file_bytes
        self.total_bytes = max(file_bytes, total_bytes)
        self.retention_days = retention_days
        self.dropped = 0
        self.write_errors = 0
        self._queue = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._prefix = f"{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self._file = None
        self._file_day = None
        self._file_size = 0
        self._part = 0

    def enqueue(self, snapshot):
        with self._lock:
            if self._stop.is_set():
                self.dropped += 1
                return
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="http-capture", daemon=True)
                self._thread.start()
            try:
                self._queue.put_nowait(snapshot)
            except queue.Full:
                self.dropped += 1
                if self.dropped == 1:
                    _LOG.warning("HTTP capture queue full; snapshots are being dropped, not booking requests")

    def close(self, timeout=3):
        with self._lock:
            self._stop.set()
            worker = self._thread
        if worker:
            worker.join(timeout=timeout)
            if worker.is_alive():
                _LOG.warning("HTTP capture shutdown timed out; queued snapshots may be lost")

    def _prune(self, incoming):
        root = self.directory.resolve()
        files = []
        for path in self.directory.glob("http_*.jsonl"):
            if path.is_symlink() or path.resolve().parent != root or not path.is_file():
                continue
            stat = path.stat()
            files.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in files)
        cutoff = time.time() - self.retention_days * 86400
        for modified, size, path in sorted(files):
            if path == self._file:
                continue
            if modified < cutoff or total + incoming > self.total_bytes:
                path.unlink()
                total -= size

    def _write(self, snapshot):
        record = _serialize(snapshot)
        record["capture_dropped"] = self.dropped
        record["capture_write_errors"] = self.write_errors
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        if len(data) > self.file_bytes:
            self.dropped += 1
            return
        day = snapshot["started_at"][:10]
        self.directory.mkdir(parents=True, exist_ok=True)
        if self._file is None or day != self._file_day or self._file_size + len(data) > self.file_bytes:
            self._part += 1
            self._file = self.directory / f"http_{day}_{self._prefix}_{self._part:04d}.jsonl"
            self._file_day, self._file_size = day, 0
        self._prune(len(data))
        with self._file.open("ab") as stream:
            stream.write(data)
        self._file_size += len(data)

    def _run(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                snapshot = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._write(snapshot)
            except Exception:
                self.write_errors += 1
                if self.write_errors == 1:
                    _LOG.warning("HTTP capture could not save a snapshot; booking continues", exc_info=False)
            finally:
                self._queue.task_done()


def _get_recorder():
    global _RECORDER
    if not _enabled():
        return None
    with _RECORDER_LOCK:
        if _RECORDER is None:
            directory = Path(__file__).resolve().parent.parent / "web_platform" / "logs" / "http_capture"
            _RECORDER = HttpCapture(
                directory,
                total_bytes=_integer_setting("BOOKING_HTTP_CAPTURE_MAX_MB", 10240, 8) * 1024 * 1024,
                retention_days=_integer_setting("BOOKING_HTTP_CAPTURE_DAYS", 30, 1),
            )
        return _RECORDER


def close_capture():
    if _RECORDER is not None:
        _RECORDER.close()


def record_request(client, method, url, *, capture_meta=None, **kwargs):
    recorder = None
    try:
        if urlsplit(str(url)).hostname == "202.117.17.144" and _path(url) in _CAPTURE_PATHS:
            recorder = _get_recorder()
    except Exception:
        _LOG.warning("HTTP capture initialization failed; booking continues")
    if recorder is None:
        return getattr(client, method.lower())(url, **kwargs)
    started_ns = time.time_ns()
    started_clock = time.perf_counter_ns()
    response, error = None, None
    try:
        response = getattr(client, method.lower())(url, **kwargs)
        return response
    except Exception as exc:
        error = exc
        raise
    finally:
        finished_ns = time.time_ns()
        elapsed_ms = (time.perf_counter_ns() - started_clock) / 1_000_000
        try:
            if response is not None and not isinstance(response, httpx.Response):
                pass
            else:
                try:
                    request = ((response.history[0].request if response.history else response.request)
                               if response is not None else getattr(error, "request", None))
                except RuntimeError:
                    request = None
                if request is None:
                    request = httpx.Request(method, url, content=kwargs.get("content", b""))
                snapshot = {
                    "schema_version": 1, "request_id": uuid.uuid4().hex,
                    "started_at": datetime.fromtimestamp(started_ns / 1e9).astimezone().isoformat(timespec="microseconds"),
                    "started_epoch_ns": started_ns, "finished_epoch_ns": finished_ns,
                    "elapsed_ms": round(elapsed_ms, 3),
                    "channel": "8080" if urlsplit(str(url)).port == 8080 else "80",
                    "context": {**_CONTEXT.get(), **(capture_meta or {})},
                    "request": {"method": request.method, "url": str(request.url),
                                **_snapshot_message(request, recorder.body_limit)},
                    "error_type": type(error).__name__ if error else None,
                }
                if response is not None:
                    snapshot["response"] = {"status_code": response.status_code, "url": str(response.url),
                                            **_snapshot_message(response, recorder.body_limit)}
                    snapshot["redirects"] = [
                        {"url": str(hop.url), "method": hop.request.method,
                         "status_code": hop.status_code, "headers": dict(hop.headers)}
                        for hop in response.history
                    ]
                recorder.enqueue(snapshot)
        except Exception:
            _LOG.warning("HTTP capture snapshot failed; booking continues", exc_info=False)


atexit.register(close_capture)
