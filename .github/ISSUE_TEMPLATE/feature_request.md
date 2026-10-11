---
name: Feature request
about: Suggest a capability or change for Livewall
title: ""
labels: enhancement
assignees: ""
---

<!--
Livewall is deliberately small. For a behaviour change, an issue first is the
right move so the design can be discussed before any code is written.
-->

## The problem

What are you trying to do, and where does Livewall get in the way today? A
concrete use case is worth more than an abstract one.

## Proposed solution

What you would like to see. If it changes the board spec, the HTTP API, the MCP
tools or the CLI, sketch the shape (a request, a tool call, a block, a flag).

## Which surface

- [ ] Backend (HTTP API / SSE)
- [ ] Web client
- [ ] MCP server
- [ ] CLI
- [ ] Docs / examples only
- [ ] Not sure

## Alternatives considered

Other ways to solve it, and why they are worse.

## Fit with the project's rules

Livewall holds a few rules that are not negotiable (see
[CONTRIBUTING.md](../../CONTRIBUTING.md)). Confirm the request can respect them:

- [ ] It does not introduce a silent failure (a write takes effect or is refused
      by name).
- [ ] It validates before it mutates.
- [ ] It keeps one contract shared by the HTTP API and the MCP server.
- [ ] It keeps boards ephemeral — no new persistence is required.

## Checks

- [ ] I searched existing issues for this.
- [ ] This is not a security report (those go through [SECURITY.md](../../SECURITY.md)).
