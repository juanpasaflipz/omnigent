"""Derive the server bind host and public base URL from the PaaS environment.

The OSS Docker entrypoint (``deploy/docker/entrypoint.py``) runs as a
side-effecting boot script, so its host / URL derivation can't be unit-tested
in place. These pure helpers hold that logic so it is testable and reusable,
and so the per-platform quirks (Railway's IPv6 bind, each provider's
public-URL variable) live in one documented spot.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from urllib.parse import urlsplit


def _is_railway(environ: Mapping[str, str]) -> bool:
    """
    Detect whether the process is running on Railway.

    Railway injects several ``RAILWAY_``-prefixed variables (e.g.
    ``RAILWAY_PUBLIC_DOMAIN``, ``RAILWAY_PROJECT_ID``); the presence of any of
    them is a reliable signal.

    :param environ: The process environment, e.g. ``os.environ``.
    :returns: True when running on Railway.
    """
    return any(key.startswith("RAILWAY_") for key in environ)


def resolve_bind_host(
    configured_host: str | None,
    environ: Mapping[str, str],
    *,
    default: str = "0.0.0.0",
) -> str:
    """
    Resolve the address uvicorn should bind, adjusting for PaaS quirks.

    The explicitly configured host (from the YAML config or the ``HOST`` env
    var) wins, else ``default``. Two platform adjustments are then applied:

    - Strip brackets from the IPv6 URL form ``"[::]"`` → ``"::"`` — some
      platforms (e.g. Railway) inject the bracketed form, which a socket bind
      rejects with ``getaddrinfo`` errors.
    - On Railway, coerce the IPv6 wildcard ``"::"`` to the IPv4 wildcard
      ``"0.0.0.0"``. Railway injects ``HOST=[::]`` but its edge proxy reaches
      the app over IPv4, so binding the IPv6 wildcard drops all traffic and
      fails health checks. Gated on Railway so a deliberate ``HOST=::`` on
      other platforms is preserved.

    :param configured_host: Host from the config file or ``HOST`` env var, or
        None when unset, e.g. ``"0.0.0.0"`` or ``"[::]"``.
    :param environ: The process environment, used to detect the platform,
        e.g. ``os.environ``.
    :param default: Bind address when none is configured, e.g. ``"0.0.0.0"``.
    :returns: The host string to bind, e.g. ``"0.0.0.0"``.
    """
    host = configured_host or default
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host == "::" and _is_railway(environ):
        host = "0.0.0.0"
    return host


def paas_public_base_url(environ: Mapping[str, str]) -> str | None:
    """
    Return the public base URL a PaaS platform injected, or ``None``.

    Checks each platform's variable in turn:

    - Render: ``RENDER_EXTERNAL_URL`` (already a full ``https://`` URL).
    - Railway: ``RAILWAY_PUBLIC_DOMAIN`` (host only).
    - Fly.io: ``FLY_APP_NAME`` (→ ``https://<app>.fly.dev``).
    - Hugging Face Spaces: ``SPACE_HOST`` (host only, e.g.
      ``"user-space.hf.space"``).

    :param environ: The process environment, e.g. ``os.environ``.
    :returns: The public base URL, e.g. ``"https://myapp.onrender.com"``, or
        ``None`` when no platform variable is set.
    """
    render_url = environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        return render_url
    railway_host = environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_host:
        return f"https://{railway_host}"
    fly_app = environ.get("FLY_APP_NAME")
    if fly_app:
        return f"https://{fly_app}.fly.dev"
    hf_host = environ.get("SPACE_HOST")
    if hf_host:
        return f"https://{hf_host}"
    return None


def detect_base_url(
    environ: Mapping[str, str],
    *,
    host: str,
    port: int,
) -> str:
    """
    Derive the server's public base URL from the PaaS environment.

    So a 1-click deploy needs zero manual ``OMNIGENT_ACCOUNTS_BASE_URL``
    config. Uses :func:`paas_public_base_url`, falling back to the bind
    address for local / Docker / EC2.

    :param environ: The process environment, e.g. ``os.environ``.
    :param host: The resolved bind host, used only for the local fallback,
        e.g. ``"0.0.0.0"``.
    :param port: The bind port, used only for the local fallback,
        e.g. ``8000``.
    :returns: The public base URL, e.g. ``"https://myapp.onrender.com"`` or
        ``"http://0.0.0.0:8000"``.
    """
    return paas_public_base_url(environ) or f"http://{host}:{port}"


def is_public_base_url(url: str) -> bool:
    """
    Whether *url* names a host that the internet (e.g. GitHub) can reach.

    Rejects non-HTTP(S) URLs, ``localhost`` names, and loopback, private,
    link-local or unspecified IP literals such as ``0.0.0.0``.

    :param url: A base URL, e.g. ``"https://omnigent.example.com"``.
    :returns: ``True`` for a plausibly public URL.
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
    )


def resolve_public_base_url(
    environ: Mapping[str, str], request_base_url: str | None = None
) -> tuple[str | None, str | None]:
    """
    Pick the server's internet-reachable base URL for absolute callback links.

    Candidates, in order: ``OMNIGENT_ACCOUNTS_BASE_URL`` (which the Docker
    entrypoint fills from :func:`detect_base_url`), ``https://$OMNIGENT_DOMAIN``,
    the PaaS variables (:func:`paas_public_base_url`), then the URL the
    current request arrived on. The first public one wins.

    :param environ: The process environment, e.g. ``os.environ``.
    :param request_base_url: Origin (plus any deployment base path) of the
        current request, e.g. ``"https://omnigent.example.com"``.
    :returns: ``(public_url, best_candidate)``: the first public candidate
        without a trailing slash (``None`` when none is public), and the first
        candidate seen at all (to name in an error message).
    """
    domain = environ.get("OMNIGENT_DOMAIN", "").strip()
    candidates = [
        environ.get("OMNIGENT_ACCOUNTS_BASE_URL", "").strip(),
        f"https://{domain}" if domain else "",
        paas_public_base_url(environ) or "",
        request_base_url or "",
    ]
    seen = [candidate.rstrip("/") for candidate in candidates if candidate]
    for candidate in seen:
        if is_public_base_url(candidate):
            return candidate, candidate
    return None, seen[0] if seen else None
