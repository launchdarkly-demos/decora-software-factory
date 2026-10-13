"""Small LaunchDarkly REST client. The factory does not depend on the MCP session."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from factory.errors import FactoryError
from factory.launchdarkly.token import scrub

BASE_URL = "https://app.launchdarkly.com/api/v2"
UrlOpen = Callable[..., Any]


class LaunchDarklyClient:
    def __init__(
        self,
        token: str,
        *,
        urlopen: UrlOpen | None = None,
        base_url: str = BASE_URL,
        timeout: float = 20,
    ) -> None:
        if not token:
            raise FactoryError("LAUNCHDARKLY_API_TOKEN is unset.")
        self._token = token
        self._urlopen = urlopen or urllib.request.urlopen
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        semantic: bool = False,
        query: list[tuple[str, str]] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        url = self._base + path
        if query:
            url = url + "?" + urllib.parse.urlencode(query)
        data: bytes | None = None
        headers = {
            "Authorization": self._token,
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            if semantic:
                headers["Content-Type"] = "application/json; domain-model=launchdarkly.semanticpatch"
            else:
                headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._urlopen(req, timeout=self._timeout) as response:
                status = int(getattr(response, "status", 0) or response.getcode())
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read()
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise FactoryError(self._public(f"LaunchDarkly request failed: {reason}")) from None
        payload = _decode(raw)
        if status >= 400 and status not in {404, 409}:
            message = ""
            if isinstance(payload, dict):
                message = str(payload.get("message") or payload.get("code") or "")
            raise FactoryError(self._public(f"LaunchDarkly API error ({status}): {message[:300]}"))
        if not isinstance(payload, dict):
            payload = {"message": str(payload)}
        return status, payload

    def _public(self, text: str) -> str:
        return scrub(text, self._token)


def _decode(raw: bytes | str | None) -> Any:
    if raw is None:
        return {}
    if isinstance(raw, str):
        text = raw
    else:
        text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"message": text[:300]}
