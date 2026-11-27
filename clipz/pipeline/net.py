"""HTTP clients. Kick traffic goes through one place so it can use a proxy."""
from __future__ import annotations

import httpx

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def http_client(timeout: float = 30.0, proxy: str | None = None, headers: dict | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        proxy=proxy or None,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    )


def kick_client(proxy: str | None = None, timeout: float = 30.0) -> httpx.Client:
    """Client for kick.com's site API. Set KICK_PROXY if the server's IP is blocked."""
    return http_client(
        timeout=timeout,
        proxy=proxy,
        headers={"Accept": "application/json", "Referer": "https://kick.com/"},
    )
