## What this changes

A short description of the change and why.

## Checklist

- [ ] `make test` passes (backend, CLI, MCP and web suites).
- [ ] `cd web && npm run lint` passes (oxlint), if the client changed.
- [ ] A test covers the change, and I watched it fail before the fix and pass
      after (revert the source, watch it go red, restore it).
- [ ] If a number, name, path, flag or field changed, the docs that state it were
      updated in the same PR.
- [ ] If the vocabulary, limits or validators changed, they changed once in
      `backend/contract.py` (shared by the HTTP API and the MCP server).
- [ ] Numbers still render exact by default; server and client formatters agree
      (`format_parity`).
- [ ] No new bare `except:` / `except Exception` in product code.
- [ ] Commits are focused and the messages say what changed and why.

## Related issue

Closes #
