import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import requests


class RemoteError(Exception):
    def __init__(self, status=None, delay=0):
        self.status = status
        self.delay = delay
        super().__init__(f"HTTP {status}" if status else "Error de conexión")


def retry_delay(value, default):
    try:
        return max(1, min(86400, int(value)))
    except (ValueError, TypeError):
        try:
            return max(
                1,
                min(
                    86400,
                    int(
                        (
                            parsedate_to_datetime(value) - datetime.now(timezone.utc)
                        ).total_seconds()
                    ),
                ),
            )
        except (ValueError, TypeError, OverflowError):
            return default


class Client:
    def __init__(self, settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "PokemonMonitor/1.0", "Accept": "application/json"}
        )
        self.blocked_until = 0
        self.blocked_status = 429
        self.last_request = None

    def get(self, url, params=None):
        if time.time() < self.blocked_until:
            raise RemoteError(
                self.blocked_status, int(self.blocked_until - time.time())
            )
        for attempt in range(self.settings.retries + 1):
            if self.last_request is not None:
                remaining = self.settings.request_delay - (
                    time.monotonic() - self.last_request
                )
                if remaining > 0:
                    time.sleep(remaining)
            self.last_request = time.monotonic()
            try:
                r = self.session.get(url, params=params, timeout=self.settings.timeout)
            except requests.RequestException:
                if attempt == self.settings.retries:
                    raise RemoteError() from None
                time.sleep(min(2 ** (attempt + 1), 30))
                continue
            if r.status_code == 416:
                return None
            if r.status_code == 429:
                self.blocked_status = 429
                delay = retry_delay(r.headers.get("Retry-After"), 60)
                self.blocked_until = time.time() + delay
                raise RemoteError(429, delay)
            if (
                r.status_code in (500, 502, 503, 504)
                and attempt < self.settings.retries
            ):
                time.sleep(min(2 ** (attempt + 1), 30))
                continue
            if not r.ok:
                if r.status_code in (401, 403):
                    self.blocked_until = time.time() + 300
                    self.blocked_status = r.status_code
                raise RemoteError(r.status_code)
            try:
                return r.json()
            except ValueError:
                raise RemoteError() from None
