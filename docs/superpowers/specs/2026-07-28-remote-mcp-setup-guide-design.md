# Remote MCP Setup Guide Design

**Status:** Approved in conversation on 2026-07-28

**Target document:** `docs/remote-mcp-setup.md`

## Problem

The README currently explains the remote project MCP at a high level, while the deployment
guide contains server-oriented configuration and verification notes. A user who only wants to
connect an AI client must combine information from both documents and still lacks
client-specific examples.

Vibe Typst needs one short, task-oriented setup guide that takes a user from creating a
Personal Access Token to making a verified MCP tool call.

## Audience and Goal

The guide is for an existing Vibe Typst user who has permission to sign in and create a
Personal Access Token. It is not a server deployment guide.

After following it, the user should have:

1. created an appropriately scoped Viewer or Editor token;
2. configured an MCP client to connect over Streamable HTTP;
3. verified the connection with `list_projects`; and
4. understood how to revoke or replace the token safely.

## Canonical Service Address

Every example uses the production service:

```text
https://vibetypst.yjwspace.win/mcp
```

The guide does not use placeholder domains in copyable client configurations. It explains that
the browser site and MCP endpoint share the same origin, with MCP served at `/mcp`.

## Chosen Documentation Structure

Create `docs/remote-mcp-setup.md` with these sections:

1. **Prerequisites** — a Vibe Typst account and an MCP client that supports Streamable HTTP and
   bearer headers.
2. **Create a token** — open Personal Access Tokens, choose Viewer or Editor, set an expiry,
   copy the one-time secret, and keep it out of source control.
3. **Choose permissions** — concise Viewer versus Editor comparison.
4. **Install in a client** — copyable configurations for:
   - a generic Streamable HTTP MCP client;
   - Codex;
   - Claude Code;
   - Claude Desktop when its current client supports the required remote transport directly,
     or the officially supported connector flow otherwise.
5. **Verify the connection** — list tools, call `list_projects`, open a project, and confirm that
   a returned browser URL contains no bearer token.
6. **Typical workflow** — `list_projects` or create, `open_project`, retain the returned
   `project_handle`, then use project-scoped tools.
7. **Troubleshooting** — 401 authentication failures, revoked or expired tokens, insufficient
   Viewer scope, stale project handles, and clients that do not expand environment variables.
8. **Security and removal** — revoke tokens in the account UI and remove the client
   configuration.

The README's existing remote-MCP section remains a short overview and gains a prominent link to
the new guide. Server setup stays in `docs/deployment.md`.

## Accuracy Rules

- Verify Codex instructions against current official OpenAI documentation before writing the
  client example.
- Verify Claude client instructions against current official Anthropic documentation before
  writing the client example.
- Clearly distinguish JSON configuration from shell commands.
- Do not imply that all MCP clients use the same environment-variable syntax.
- Never include a real token, cookie, password, or project handle.
- Use `vbt_...` only as a visibly incomplete example secret.

## Validation

Before committing the setup guide:

- check every internal Markdown link;
- confirm every copied service URL uses HTTPS and the canonical production domain;
- scan for accidental secrets and placeholder hosts;
- test the documented generic MCP connection against production using a short-lived token;
- revoke and remove the verification token afterward; and
- ensure the existing README and deployment documentation remain consistent with the guide.

## Non-goals

- Re-documenting server installation or Cloudflare configuration.
- Listing all remote MCP tools and schemas; those remain discoverable through `tools/list`.
- Teaching general MCP protocol development.
- Adding screenshots or maintaining separate guides per client.
