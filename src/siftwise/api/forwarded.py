"""Forwarded headers from trusted proxies: the public scheme, client and host.

uvicorn's own ``ProxyHeadersMiddleware`` sets the scheme and client from
``X-Forwarded-Proto``/``-For`` when the peer is in ``--forwarded-allow-ips``, but ignores
``X-Forwarded-Host``. The dashboard's Origin check and its cookie's ``Secure`` flag need the
public origin (``https://siftwise.example.com``), not the address the proxy connected to, so
this middleware does uvicorn's work plus the host, with the same trust list:
``SIFTWISE_FORWARDED_ALLOW_IPS`` (``siftwise server start --forwarded-allow-ips``). A peer
not in the list has every forwarded header ignored. ``siftwise server start`` turns uvicorn's
own copy off, so the peer this sees is still the proxy.
"""

from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

FORWARDED_HOST = "siftwise.forwarded_host"  # scope key: the public host, from a trusted proxy


class ForwardedMiddleware(ProxyHeadersMiddleware):
    def __init__(self, app: ASGIApp, trusted_hosts: str) -> None:
        super().__init__(app, trusted_hosts)  # type: ignore[arg-type]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # type: ignore[override]
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            if client and client[0] in self.trusted_hosts:
                for name, value in scope["headers"]:
                    if name == b"x-forwarded-host":
                        host = value.decode("latin1").split(",")[0].strip()
                        if host:
                            scope[FORWARDED_HOST] = host
                        break
        await super().__call__(scope, receive, send)  # type: ignore[arg-type]


def public_origin(scope: Scope) -> tuple[str, str]:
    """``(scheme, host)`` as the browser sees them: a trusted proxy's forwarded host and
    scheme, else this request's own."""
    host = scope.get(FORWARDED_HOST)
    if not host:
        host = next((v.decode("latin1") for k, v in scope["headers"] if k == b"host"), "")
    return str(scope.get("scheme", "http")), host.strip().lower()
