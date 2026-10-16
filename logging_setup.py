"""Structured JSON logging and direct Loki delivery."""

import json
import logging
import os
import queue
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone


_STANDARD_RECORD_ATTRIBUTES = {
    'args',
    'asctime',
    'created',
    'exc_info',
    'exc_text',
    'filename',
    'funcName',
    'levelname',
    'levelno',
    'lineno',
    'message',
    'module',
    'msecs',
    'msg',
    'name',
    'pathname',
    'process',
    'processName',
    'relativeCreated',
    'stack_info',
    'taskName',
    'thread',
    'threadName',
}


class JsonFormatter(logging.Formatter):
    """Format one LogRecord as one JSON line."""

    def format(self, record):
        payload = {
            'ts': datetime.fromtimestamp(
                record.created,
                timezone.utc,
            ).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'level': record.levelname,
            'logger': record.name,
            'event': getattr(record, 'event', record.getMessage()),
        }

        for key, value in record.__dict__.items():
            if (
                key in _STANDARD_RECORD_ATTRIBUTES
                or key == 'event'
                or key.startswith('_')
            ):
                continue
            payload[key] = value

        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        if record.stack_info:
            payload['stack'] = self.formatStack(record.stack_info)

        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(',', ':'),
            default=str,
        )


class LokiHandler(logging.Handler):
    """Buffer JSON logs and push them directly to Loki from a daemon thread."""

    def __init__(
        self,
        url,
        *,
        app_name='rmb03',
        batch_size=100,
        flush_interval=1.0,
        timeout=0.8,
        transport=None,
        start_worker=True,
    ):
        super().__init__()
        self.url = self._push_url(url)
        self.app_name = app_name
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.1, float(flush_interval))
        self.timeout = min(max(0.1, float(timeout)), 1.0)
        self._transport = transport or self._post_payload
        self._queue = queue.Queue(maxsize=max(1000, self.batch_size * 10))
        self._stop_event = threading.Event()
        self._flush_lock = threading.Lock()
        self._worker = None
        self.setFormatter(JsonFormatter())

        if start_worker:
            self._worker = threading.Thread(
                target=self._run_worker,
                name='rmb03-loki-handler',
                daemon=True,
            )
            self._worker.start()

    @staticmethod
    def _push_url(url):
        base = url.strip().rstrip('/')
        suffix = '/loki/api/v1/push'
        if base.endswith(suffix):
            return base
        return base + suffix

    def emit(self, record):
        try:
            item = (time.time_ns(), self.format(record), record.levelname.upper())
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    pass
        except Exception:
            self.handleError(record)

    def flush(self):
        batch = self._drain_queue()
        if batch:
            self._send_batch(batch)

    def close(self):
        if getattr(self, '_closed', False):
            return

        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=self.timeout + 1.0)
        try:
            self.flush()
        except Exception:
            pass
        super().close()

    def _run_worker(self):
        batch = []
        deadline = time.monotonic() + self.flush_interval

        while not self._stop_event.is_set():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                item = self._queue.get(
                    timeout=min(remaining, 0.2) if remaining else 0,
                )
            except queue.Empty:
                if batch and time.monotonic() >= deadline:
                    self._send_batch(batch)
                    batch = []
                    deadline = time.monotonic() + self.flush_interval
                elif not batch:
                    deadline = time.monotonic() + self.flush_interval
                continue

            batch.append(item)
            if len(batch) >= self.batch_size:
                self._send_batch(batch)
                batch = []
                deadline = time.monotonic() + self.flush_interval

        batch.extend(self._drain_queue())
        if batch:
            self._send_batch(batch)

    def _drain_queue(self):
        batch = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                return batch

    def _send_batch(self, batch):
        streams = {}
        for timestamp_ns, line, level in batch:
            streams.setdefault(level, []).append([str(timestamp_ns), line])

        payload = {
            'streams': [
                {
                    'stream': {'app': self.app_name, 'level': level},
                    'values': values,
                }
                for level, values in streams.items()
            ]
        }

        # Logging must never make the business request fail, even if Loki is down.
        try:
            with self._flush_lock:
                self._transport(payload)
        except Exception:
            pass

    def _post_payload(self, payload):
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode('utf-8')
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            response.read()


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def setup_logging():
    """Configure root and Werkzeug loggers as JSON, optionally pushing to Loki."""

    level_name = os.environ.get('RMB03_LOG_LEVEL', 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = JsonFormatter()
    root_logger = logging.getLogger()
    werkzeug_logger = logging.getLogger('werkzeug')

    for logger in (root_logger, werkzeug_logger):
        for handler in list(logger.handlers):
            if getattr(handler, '_rmb03_managed', False):
                logger.removeHandler(handler)
                handler.close()

    handlers = []
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler._rmb03_managed = True
    handlers.append(stdout_handler)

    loki_url = os.environ.get('RMB03_LOKI_URL', '').strip()
    if loki_url:
        loki_handler = LokiHandler(
            loki_url,
            batch_size=_env_int('RMB03_LOKI_BATCH_SIZE', 100),
            flush_interval=_env_float('RMB03_LOKI_FLUSH_INTERVAL', 1.0),
            timeout=_env_float('RMB03_LOKI_TIMEOUT', 0.8),
        )
        loki_handler.setFormatter(formatter)
        loki_handler._rmb03_managed = True
        handlers.append(loki_handler)

    root_logger.setLevel(level)
    for handler in handlers:
        root_logger.addHandler(handler)

    werkzeug_logger.setLevel(level)
    werkzeug_logger.propagate = False
    for handler in handlers:
        werkzeug_logger.addHandler(handler)

    return root_logger
