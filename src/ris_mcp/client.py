"""HTTP-Client fuer die RIS-OGD-API.

Verantwortlichkeiten:

* Asynchrone Requests via ``httpx.AsyncClient``.
* Token-Bucket Rate-Limit, damit wir die RIS-API nicht ueberrennen.
* Exponentielles Backoff bei 5xx und Netzwerkfehlern via tenacity.
* Conditional Requests: ETag / Last-Modified aus dem Cache mitschicken
  und 304-Antworten erkennen.
* Strukturiertes Logging via structlog.

Die Methode ``search`` ist der Hauptzweck: einen Suchcall gegen einen
Controller (``Bundesrecht``, ``Judikatur``, ...) absetzen und das JSON
liefern. Volltexte (XML/HTML/PDF/RTF) liefert ``fetch_content``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .cache import Cache
from .config import Settings

logger = structlog.get_logger(__name__)


class RisApiError(RuntimeError):
    """Allgemeiner RIS-Fehler (4xx/5xx, Validierung, Timeouts nach Retry)."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class _TokenBucket:
    """Einfacher Async-Token-Bucket fuer Rate-Limiting.

    Refill mit ``rate`` Tokens pro Sekunde, maximaler Bucket-Inhalt = ``burst``.
    Wer keinen Token bekommt, schlaeft so lange, bis einer da ist.
    """

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, amount: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity, self.tokens + (now - self.updated_at) * self.rate
                )
                self.updated_at = now
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
                deficit = amount - self.tokens
                wait_s = deficit / self.rate
            await asyncio.sleep(wait_s)


class RisClient:
    """Async-Client fuer die RIS-OGD-API.

    Wird im FastMCP-Lifecycle einmal erstellt und beim Shutdown geschlossen.
    """

    def __init__(self, settings: Settings, cache: Cache | None = None) -> None:
        self.settings = settings
        self.cache = cache
        self._bucket = _TokenBucket(
            rate=settings.rate_limit_per_second, burst=settings.rate_limit_burst
        )
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.settings.ris_base_url,
            timeout=self.settings.http_timeout_seconds,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "application/json",
                "Accept-Charset": "utf-8",
            },
            follow_redirects=True,
        )
        logger.info("ris_client.started", base_url=self.settings.ris_base_url)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("RisClient not started. Call start() first.")
        return self._client

    # ------------------------------------------------------------------ search
    async def search(
        self,
        controller: str,
        application: str,
        params: dict[str, Any],
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Suchabfrage gegen einen RIS-Controller.

        ``controller`` ist z. B. ``"Bundesrecht"`` oder ``"Judikatur"``;
        ``application`` der ``Applikation``-Parameter (``"BrKons"``,
        ``"Justiz"``, ``"Vfgh"``, ...). Restliche Params werden 1:1 in den
        Querystring uebernommen.
        """
        merged = {"Applikation": application, **params}

        if self.cache and not force_refresh:
            cached = await self.cache.get_search(controller, application, merged)
            if cached is not None:
                logger.debug("ris.cache_hit", controller=controller, application=application)
                return cached

        path = f"/{controller}"
        response = await self._request_with_retry("GET", path, params=merged)
        data = response.json()

        if self.cache:
            await self.cache.put_search(
                controller=controller,
                application=application,
                params=merged,
                response=data,
                ttl_seconds=self.settings.ttl_search_seconds,
            )
        return data

    # ------------------------------------------------------------------ content
    async def fetch_content(self, url: str) -> tuple[bytes, str]:
        """Volltext-Inhalt (XML/HTML/PDF/RTF) abholen.

        Liefert ``(raw_bytes, content_type)``. Nutzt 304-Conditional-Requests,
        wenn wir fuer die URL bereits ETag oder Last-Modified im Cache haben.
        """
        headers: dict[str, str] = {"Accept": "*/*"}
        if self.cache:
            meta = await self.cache.get_http_meta(url)
            if meta:
                if meta.get("etag"):
                    headers["If-None-Match"] = meta["etag"]
                if meta.get("last_modified"):
                    headers["If-Modified-Since"] = meta["last_modified"]

        await self._bucket.acquire()
        client = self._require_client()
        try:
            resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise RisApiError(f"Network error: {exc}", url=url) from exc

        if resp.status_code == 304:
            logger.debug("ris.content_not_modified", url=url)
            raise RisApiError("Content not modified", status=304, url=url)
        if resp.status_code >= 400:
            raise RisApiError(
                f"RIS content fetch failed: HTTP {resp.status_code}",
                status=resp.status_code,
                url=url,
            )

        if self.cache:
            await self.cache.put_http_meta(
                url=url,
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
                last_status=resp.status_code,
            )

        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        return resp.content, content_type

    # ------------------------------------------------------------------ internals
    async def _request_with_retry(
        self, method: str, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        client = self._require_client()
        retry_exceptions = (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        )

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
            retry=retry_if_exception_type(retry_exceptions),
            reraise=True,
        ):
            with attempt:
                await self._bucket.acquire()
                logger.debug(
                    "ris.request",
                    method=method,
                    path=path,
                    attempt=attempt.retry_state.attempt_number,
                )
                resp = await client.request(method, path, params=params)
                # 5xx => Retry; 4xx => direkt durchreichen
                if 500 <= resp.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"Server error {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                if resp.status_code >= 400:
                    raise RisApiError(
                        f"RIS API error: HTTP {resp.status_code} {resp.text[:200]}",
                        status=resp.status_code,
                        url=str(resp.request.url),
                    )
                return resp
        # Sollte tenacity nicht erreichen, aber mypy beruhigt's.
        raise RisApiError("Unreachable: retry loop exhausted")
