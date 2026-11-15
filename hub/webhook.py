"""Opt-in delivery of alert transitions to one configured HTTP(S) URL, off the evaluation path."""
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from urllib.parse import urlsplit

from . import __version__

QUEUE_LIMIT = 256
RETRY_DELAYS = (1, 5)  # seconds before the second and third attempt


def validate_webhook(source):
    """The `webhook` object of the alert configuration: {"url": ..., "timeout_seconds": 1..30}."""
    if not isinstance(source, dict) or set(source) - {'url', 'timeout_seconds'}:
        raise ValueError('invalid webhook fields')
    url = source.get('url')
    if not isinstance(url, str) or not 0 < len(url) <= 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError('webhook url must be a URL of at most 2048 characters without spaces')
    try:
        parts = urlsplit(url)
        parts.port  # raises on an invalid port
    except ValueError:
        raise ValueError('invalid webhook url') from None
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError('webhook url must be http:// or https:// with a host')
    if parts.username is not None or parts.password is not None:
        raise ValueError('webhook url must not carry a user name or password')
    timeout = source.get('timeout_seconds', 5)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 30:
        raise ValueError('webhook timeout_seconds must be 1..30')
    return {'url': url, 'timeout_seconds': timeout}


def target(url):
    """What diagnostics may show of the URL: scheme and host, never the path or query that may hold a token."""
    parts = urlsplit(url)
    return '%s://%s' % (parts.scheme, parts.netloc)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises the 3xx as an HTTPError


class WebhookSender:
    """One worker thread posts queued transitions; submit() never blocks or raises."""

    def __init__(self, config, *, retry_delays=RETRY_DELAYS, wall=time.time):
        self.url, self.timeout = config['url'], config['timeout_seconds']
        self.retry_delays, self.wall = tuple(retry_delays), wall
        # Hub never proxies Agent traffic either: no environment proxy, no redirects.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._queue = deque()
        self._cond = threading.Condition()
        self._stop = False
        self._thread = None
        self._busy = False
        self.delivered = self.failed = self.dropped = 0
        self.last_error = None
        self.last_delivered_ms = self.last_failed_ms = None

    def submit(self, payload):
        with self._cond:
            if len(self._queue) >= QUEUE_LIMIT:
                self._queue.popleft()  # the oldest transition gives way; the count says so
                self.dropped += 1
            self._queue.append(payload)
            self._cond.notify()

    def snapshot(self):
        with self._cond:
            return dict(target=target(self.url), queued=len(self._queue) + self._busy, delivered=self.delivered,
                        failed=self.failed, dropped=self.dropped, last_error=self.last_error,
                        last_delivered_ms=self.last_delivered_ms, last_failed_ms=self.last_failed_ms)

    def _post(self, payload):
        body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        request = urllib.request.Request(self.url, data=body, method='POST', headers={
            'Content-Type': 'application/json', 'User-Agent': 'platmon-hub/' + __version__})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                response.read(65536)
                return None if 200 <= response.status < 300 else 'http_%d' % response.status
        except urllib.error.HTTPError as exc:
            exc.close()
            return 'http_%d' % exc.code
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            reason = getattr(exc, 'reason', exc)
            return 'timeout' if isinstance(reason, TimeoutError) or 'timed out' in str(reason) else 'connection'
        except Exception:
            return 'error'

    def _deliver(self, payload):
        error = None
        for attempt in range(len(self.retry_delays) + 1):
            if attempt:
                with self._cond:
                    if self._cond.wait_for(lambda: self._stop, self.retry_delays[attempt - 1]):
                        return error or 'stopped'
            error = self._post(payload)
            if error is None:
                return None
        return error

    def _run(self):
        log = logging.getLogger(__name__)
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._stop or self._queue)
                if self._stop:
                    return
                payload = self._queue.popleft()
                self._busy = True
            error = self._deliver(payload)
            with self._cond:
                self._busy = False
                now = int(self.wall() * 1000)
                if error is None:
                    self.delivered += 1
                    self.last_delivered_ms = now
                else:
                    if error != self.last_error:
                        log.warning('alert webhook delivery to %s failed: %s', target(self.url), error)
                    self.failed += 1
                    self.last_failed_ms = now
                self.last_error = error

    def start(self):
        self._thread = threading.Thread(target=self._run, name='hub-alert-webhook', daemon=True)
        self._thread.start()

    def close(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout + 1)
