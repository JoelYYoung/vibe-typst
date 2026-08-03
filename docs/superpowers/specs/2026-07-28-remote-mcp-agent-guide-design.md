# Remote MCP Agent Guide Design

**Status:** Approved in conversation on 2026-07-28

**Target document:** `docs/remote-mcp-agent-guide.md`

## Problem

The README currently explains the remote project MCP at a high level, while the deployment
guide contains server-oriented configuration and verification notes. Neither document gives an
AI agent a compact operating contract for using the MCP safely and effectively.

Vibe Typst needs one short, task-oriented guide that a human can give to an agent together with
the MCP endpoint and a securely configured token.

## Audience and Goal

The guide is written for the remote AI agent, not for the human account owner or server
administrator. It assumes the human has already created a Personal Access Token and made it
available through the agent platform's secret facility.

After following it, the agent should have:

1. verified that the MCP connection works;
2. discovered the available tools rather than assuming a hard-coded tool list;
3. selected or created a project and retained its opaque project handle;
4. used the correct workflow for Typst or PDF projects; and
5. recovered correctly when authorization, scope, or project context changes.

## Canonical Service Address

Every example uses the production service:

```text
https://vibetypst.yjwspace.win/mcp
```

The guide states that every MCP request uses Streamable HTTP with:

```http
Authorization: Bearer <VIBE_TYPST_TOKEN>
```

It does not describe Cloudflare, server environment variables, or client settings screens.

## Chosen Documentation Structure

Create `docs/remote-mcp-agent-guide.md` with these sections:

1. **Connection contract** — canonical endpoint, Streamable HTTP, bearer authentication, and
   rules that the agent must never print, persist, or place the token in a project.
2. **First actions** — initialize MCP, discover tools, call `list_projects`, then either select
   an existing project or create the requested project type.
3. **Project handles** — call `open_project`, retain the returned opaque `project_handle`, pass
   it to every project-scoped tool, and share only the returned `web_url` with the human.
4. **Typst workflow** — require Touying and keep all Typst presentation source in `main.typ`;
   forbid auxiliary `.typ` files and local `.typ` imports/includes; read and search the live
   document, use structured edit tools instead of overwriting it, inspect previews, process Typst
   comments, and export when requested.
5. **PDF workflow** — inspect PDF metadata and extracted text, read or update per-page
   transcripts, inspect rendered pages, and use the managed PDF replacement flow. Explicitly
   state that PDF projects have no comment workflow and that the agent must not edit managed PDF
   files through generic file tools.
6. **Ordinary files and transfers** — use text tools for small text files and the bounded
   begin/upload-or-download/finish flow for binary or larger files.
7. **Human collaboration** — keep the server project as the source of truth and return the
   authenticated `web_url` when the human needs to inspect progress.
8. **Error recovery** — handle expired or revoked tokens, Viewer scope denial, stale project
   handles, active-project changes, upload expiry, and file conflicts without retrying unsafe
   mutations blindly.
9. **Completion checklist** — verify the final project, preview, transcript/comment state, and
   human link before reporting completion.

The README's existing remote-MCP section remains a short human-facing overview and gains a
prominent link labelled as the agent usage guide. Server setup stays in `docs/deployment.md`.

## Accuracy Rules

- Derive tool names, arguments, returned fields, and error codes from the implemented MCP
  service and tests.
- Tell the agent to use MCP tool discovery because future deployments may add tools.
- Separate required operating rules from optional examples.
- Keep all instructions client-neutral and directly actionable by an agent.
- Never include a real token, cookie, password, or project handle.
- Use `vbt_...` only as a visibly incomplete example secret.

## Validation

Before committing the agent guide:

- check every internal Markdown link;
- confirm the service URL uses HTTPS and the canonical production domain;
- scan for accidental secrets and placeholder hosts;
- verify every named workflow against the current tool schemas and automated tests;
- test the documented core workflow against production using a short-lived token;
- revoke and remove the verification token afterward; and
- ensure the existing README and deployment documentation remain consistent with the guide.

## Non-goals

- Documenting server installation, Cloudflare configuration, or client settings screens.
- Providing Codex-, Claude-, Cursor-, or editor-specific installation steps.
- Reproducing all remote MCP schemas; those remain discoverable through `tools/list`.
- Teaching the human how to create or manage account tokens.
- Teaching general MCP protocol development.
- Adding screenshots.
