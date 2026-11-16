"""Serial, rate-limited HTTP access to SEC data."""

from email.utils import parsedate_to_datetime
import json
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import DATA, WWW


class SECClient:
    """One serial client with a shared request interval and bounded retries."""

    def __init__(
        self,
        user_agent: str,
        *,
        requests_per_second: float = 5.0,
        opener: Callable = urlopen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_bytes: int = 50_000_000,
    ) -> None:
        if not user_agent.strip() or "@" not in user_agent:
            raise ValueError("Provide a descriptive SEC User-Agent with a contact email")
        if not 0 < requests_per_second <= 10:
            raise ValueError("SEC request rate must be between 0 and 10 per second")
        self.user_agent = user_agent
        self.interval = 1 / requests_per_second
        self.opener = opener
        self.sleep = sleep
        self.clock = clock
        self.max_bytes = max_bytes
        self.next_request = 0.0

    def _wait_turn(self) -> None:
        now = self.clock()
        if now < self.next_request:
            self.sleep(self.next_request - now)
        self.next_request = self.clock() + self.interval

    def get_bytes(self, url: str) -> bytes:
        if not (url.startswith(DATA + "/") or url.startswith(WWW + "/")):
            raise ValueError("Only SEC HTTPS URLs are allowed")
        request = Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept-Encoding": "identity",
            "Accept": "application/json, text/html, text/plain, */*",
        })
        for attempt in range(4):
            self._wait_turn()
            try:
                with self.opener(request, timeout=45) as response:
                    body = response.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise ValueError(f"SEC response exceeds {self.max_bytes} bytes: {url}")
                return body
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    delay = max(0.0, float(retry_after))
                except ValueError:
                    try:
                        delay = max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
                    except (TypeError, ValueError, OverflowError):
                        delay = 0.0
                self.sleep(max(delay, 2.0 ** attempt))
            except URLError:
                if attempt == 3:
                    raise
                self.sleep(2.0 ** attempt)
        raise AssertionError("unreachable")

    def get_json(self, url: str) -> dict:
        return json.loads(self.get_bytes(url))
