# Livewall documentation

This is the adopter's manual. Each page stands on its own, so you can land on any
of them directly.

Start here if you are new:

- [Getting started](getting-started.md): the end-to-end tutorial — from nothing
  to a board you can watch update, over Docker or bare metal, then wired to an
  agent.
- [Cookbook](cookbook.md): copy-paste task recipes (a migration, test results, a
  log tail, a fleet wall, a CI step, a metric over time).
- [Pushers](pushers.md): boards that update themselves — poll an endpoint, a
  command or a file and push on an interval, with no agent attached.
- [Troubleshooting](troubleshooting.md): the failure modes a new user hits, and
  the fix for each.
- [Examples](../examples/README.md): small runnable scripts you can copy.

If you are an **agent** (or wiring one up), the workflow — when to reach for a
board, the shape of the interaction, and the traps — is an Agent Skill at
[`skills/livewall/SKILL.md`](../skills/livewall/SKILL.md).

Reference:

- [Architecture](architecture.md): how the pieces fit, and why they are
  shaped that way (the SSE/transport design, the block renderer, ephemeral
  boards).
- [HTTP API](api.md): every endpoint with its method, path, request and response
  shape, status codes, and the validation rules.
- [Blocks](blocks.md): the complete block vocabulary, every type and field, what
  each renders as, and the number-formatting rule.
- [MCP](mcp.md): every MCP tool, its arguments, what it returns, and a
  multi-step example.
- [Deploying](deploying.md): Docker, bare metal, and behind a reverse proxy.
- [Protocol](protocol.md): the wire format — the SSE snapshot and the block spec.

If you are evaluating the project, start with the [README](../README.md). If you
are contributing, see [CONTRIBUTING](../CONTRIBUTING.md).
