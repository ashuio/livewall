---
name: Bug report
about: Something in Livewall is broken or behaves differently than documented
title: ""
labels: bug
assignees: ""
---

<!--
Security issue? Do NOT open a public issue — follow SECURITY.md instead.
-->

## What happened

A clear description of the bug, and the exact steps to reproduce it.

## What you expected

What you expected to happen instead.

## How you run Livewall

Check every surface involved, and say how it was started.

- [ ] Backend (HTTP API / SSE) — `docker compose up --build`, or `make backend`, or `make dev`
- [ ] Web client — served by the backend, or `cd web && npm run dev`
- [ ] MCP server — wired into an MCP host (say which)
- [ ] CLI — `livewall <command>` (`pipx install ./cli`)

## Version

The version from `README.md` / `CHANGELOG.md` (current release: `0.0.1`), and
the commit if you are on `main`:

- Livewall version / commit:

## Environment

- OS and version:
- Python version (`.venv/bin/python --version`):
- Node version (`node --version`) and package manager:
- Browser and version (for client bugs):
- How the backend is bound (loopback / `LIVEWALL_HOST` / behind a proxy):
- Is `LIVEWALL_TOKEN` set? (yes/no — never paste the token itself):

## Logs and output

Paste the relevant backend log (`LIVEWALL_LOG_LEVEL=debug` for more), browser
console output, CLI stderr, or MCP host log. Redact any token.

```
(paste here)
```

## Board spec (if the bug involves one)

The smallest board spec that reproduces it:

```json
(paste here)
```

## Checks

- [ ] I searched existing issues for this.
- [ ] This is not a security report (those go through [SECURITY.md](../../SECURITY.md)).
- [ ] I can reproduce this on the current release.
