"""Bearer-token authentication for the Livewall API.

Off unless `LIVEWALL_TOKEN` is set: a bare clone runs open (the quickstart
promises `docker compose up` and a working board), and setting the env var turns
the whole `/api/*` surface private. `/health` is never gated, so liveness probes
and the test runner's readiness check work without a credential.

Two ways to present the token, checked in this order:

  1. `Authorization: Bearer <token>`   (preferred — the client is fetch-based)
  2. `?token=<token>`                  (convenience: a browser URL, a bookmark)

A request is authorized when it carries a token that matches `LIVEWALL_TOKEN`.
The comparison is constant-time (`hmac.compare_digest` over bytes) so a wrong
token cannot be recovered by timing. When the token is unset the gate is a no-op
and every request is authorized — the *presence* of the env var is the switch,
never a value.

This module holds no FastAPI import so it can be unit-tested and reused by the
CLI's expectations; the app wires it in as a middleware.
"""
from __future__ import annotations

import hmac
import os

# The configured token. Empty string == auth disabled. Read once at import: an
# env change needs a restart, which is how every other knob here behaves.
TOKEN = os.environ.get("LIVEWALL_TOKEN") or ""


def enabled() -> bool:
    """True when a token is configured and the gate is therefore active."""
    return bool(TOKEN)


def _bearer(req) -> str | None:
    """The token from an `Authorization: Bearer <t>` header, or None.

    A non-Bearer scheme (Basic, etc.) yields None — it is not a token we accept,
    and it must not be treated as one.
    """
    header = req.headers.get("authorization")
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def candidate(req) -> str | None:
    """The token this request presents, header first then query. None if none."""
    tok = _bearer(req)
    if tok is not None:
        return tok
    return req.query_params.get("token")


def authorized(req) -> bool:
    """True if the request may proceed.

    Auth disabled -> always True. Otherwise the presented token must match,
    compared in constant time over UTF-8 bytes (so a non-ASCII token is a plain
    mismatch, not a TypeError).
    """
    if not TOKEN:
        return True
    cand = candidate(req)
    if not cand:
        return False
    return hmac.compare_digest(cand.encode("utf-8"), TOKEN.encode("utf-8"))
