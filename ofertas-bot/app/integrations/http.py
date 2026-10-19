"""Bounded HTTP to fixed official origins; no redirects, secret logs or global retries."""

import json
import math
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from app.integrations.errors import AuthorizationDenied, IntegrationError, InvalidPayload

_ORIGINS = {"api.mercadolibre.com", "open-api.affiliate.shopee.com.br"}


@dataclass(frozen=True)
class JsonResponse:
    data: dict
    fetched_at: datetime


class JsonTransport:
    def __init__(
        self,
        *,
        timeout: float = 10,
        max_retries: int = 2,
        min_interval: float = 1,
        cache_seconds: float = 30,
        max_retry_wait: float = 30,
        client: httpx.Client | None = None,
        sleep: Callable = time.sleep,
        monotonic: Callable = time.monotonic,
        clock: Callable | None = None,
        jitter: Callable = random.random,
        rate_gate: Callable | None = None,
        rate_defer: Callable | None = None,
        on_auth_failure: Callable | None = None,
    ):
        self.client = client or httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = min_interval
        self.cache_seconds = cache_seconds
        self.max_retry_wait = max_retry_wait
        self.sleep, self.monotonic, self.jitter = sleep, monotonic, jitter
        self.clock = clock or (lambda: datetime.now(UTC))
        self.rate_gate = rate_gate
        self.rate_defer, self.on_auth_failure = rate_defer, on_auth_failure
        self._cache: dict[str, tuple[float, JsonResponse]] = {}
        self._next_at = 0.0
        self._lock = threading.Lock()

    def close(self):
        self.client.close()

    def clear_cache(self):
        with self._lock:
            self._cache.clear()

    def _retry_wait(self, response: httpx.Response | None, attempt: int) -> float:
        delay = min(2**attempt + self.jitter(), self.max_retry_wait)
        if response is not None and (header := response.headers.get("Retry-After")):
            try:
                seconds = float(header)
            except ValueError:
                try:
                    seconds = (parsedate_to_datetime(header) - self.clock()).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    seconds = delay
            if not math.isfinite(seconds):
                seconds = delay
            delay = max(delay, max(0, seconds))
            if self.rate_defer is not None:
                self.rate_defer(delay)
            if seconds > self.max_retry_wait:
                # Never retry earlier than a provider's long backoff instruction.
                self._next_at = max(self._next_at, self.monotonic() + seconds)
                raise IntegrationError(
                    "RATE_LIMITED", "Provedor solicitou espera; tente mais tarde."
                )
        elif response is not None and response.status_code >= 500 and self.rate_defer is not None:
            self.rate_defer(delay)
        elif response is not None and response.status_code == 429 and self.rate_defer is not None:
            self.rate_defer(delay)
        return delay

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        body: bytes | None = None,
        cache_key: str | None = None,
        retry: bool = True,
        notify_auth_failure: bool = True,
    ) -> JsonResponse:
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname in _ORIGINS
                and parsed.port in (None, 443)
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid or method not in {"GET", "POST"}:
            raise IntegrationError("UNSAFE_REQUEST", "Destino ou método de API não autorizado.")
        # Lock/cache/rate limit is per connector+credential instance, across API operations.
        with self._lock:
            now = self.monotonic()
            if cache_key and (cached := self._cache.get(cache_key)) and cached[0] > now:
                return cached[1]
            retry_count = self.max_retries if retry else 0
            for attempt in range(retry_count + 1):
                pause = self._next_at - self.monotonic()
                if pause > self.max_retry_wait:
                    raise IntegrationError("RATE_LIMITED", "Provedor em período de espera.")
                if pause > 0:
                    self.sleep(pause)
                if self.rate_gate is not None:
                    self.rate_gate()
                self._next_at = self.monotonic() + self.min_interval
                response = None
                try:
                    response = self.client.request(
                        method,
                        url,
                        headers=headers,
                        content=body,
                        timeout=self.timeout,
                        follow_redirects=False,
                    )
                except httpx.TimeoutException:
                    error = IntegrationError("TIMEOUT", "Tempo limite da API excedido.")
                except httpx.RequestError:
                    error = IntegrationError("NETWORK_ERROR", "API temporariamente inacessível.")
                else:
                    if response.status_code in {401, 403}:
                        self._cache.clear()
                        if notify_auth_failure and self.on_auth_failure is not None:
                            self.on_auth_failure()
                        error = AuthorizationDenied()
                        error.status_code = response.status_code
                        raise error from None
                    if 200 <= response.status_code < 300:
                        if len(response.content) > 2_000_000:
                            raise InvalidPayload("Resposta excede o limite permitido.")
                        try:
                            data = json.loads(response.content, parse_float=Decimal)
                        except (ValueError, UnicodeError):
                            raise InvalidPayload() from None
                        if not isinstance(data, dict):
                            raise InvalidPayload()
                        result = JsonResponse(data, self.clock())
                        if cache_key and self.cache_seconds:
                            # Bounded cache; cached evidence keeps the original observation time.
                            if len(self._cache) >= 1000:
                                self._cache.clear()
                            self._cache[cache_key] = (self.monotonic() + self.cache_seconds, result)
                        return result
                    if response.status_code != 429 and response.status_code < 500:
                        raise IntegrationError(
                            "UPSTREAM_REJECTED", "Solicitação recusada pela API."
                        )
                    error = IntegrationError(
                        "RATE_LIMITED" if response.status_code == 429 else "UPSTREAM_ERROR",
                        "API indisponível; tente mais tarde.",
                    )
                if attempt == retry_count:
                    # Honor Retry-After even when this call has exhausted its retry budget.
                    if response is not None and (
                        response.status_code == 429 or response.status_code >= 500
                    ):
                        delay = self._retry_wait(response, attempt)
                        self._next_at = max(self._next_at, self.monotonic() + delay)
                    raise error from None
                self.sleep(self._retry_wait(response, attempt))
        raise IntegrationError("UPSTREAM_ERROR", "API indisponível.")
