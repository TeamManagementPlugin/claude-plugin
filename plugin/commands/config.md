---
description: Configure team-management (non-secret settings) through a guided menu
---

# team-management: Configure

Guide the user through team-management's **non-secret** configuration and write it
with the deterministic `config_update` MCP tool. Secrets (API tokens) are NEVER
set here — they live in the per-project file `.claude/state/provider-tokens.json`,
which the AI cannot read, and never enter this transcript.

By the time this command runs, the deterministic intent-gate hook has already
written the config session flag (you physically typed the command), so
`config_update` is unlocked for ~15 minutes. Do NOT pre-authorize `config_update`
— approve each write when prompted; the per-section batching keeps that to a
handful of prompts.

## Workflow

1. **Show current settings.** Call `mcp__plugin_team-management_tm__config_get` and summarise
   the current config to the user. Token fields appear masked (`***set***` /
   `***unset***`) — that is expected; never ask the user to paste a token here.
   The response also carries a **`schema`** array — the authoritative list of every
   settable key with its `type`, any `allowed` enum values, and a one-line
   `description`. Treat it as ground truth: consult it for a key's exact type/enum
   before every `config_update` call rather than guessing, and never try to set a key
   absent from `schema` (those are either secrets — set in the token file — or
   engine-managed state).

2. **State the token-redirect up front:**
   > Tokens (GitLab / Jira / GitHub / Telegram) are NOT configured here. Set them in
   > the per-project file `.claude/state/provider-tokens.json` — a git-ignored,
   > owner-only (0600) file the AI agent cannot read. It is created for you (with all
   > provider keys blank) the first time you run `/team-management:config` or start a
   > session; open it in your editor and fill in only the tokens you use. Each project
   > has its own file, so different projects can use different tokens.

3. **Walk the sections in order**, one `AskUserQuestion` at a time. For each
   section, first ask whether the user wants to configure it (offer a clear
   "skip — leave as-is" option). **Skip if not needed.** When they do want it,
   gather the section's values with `AskUserQuestion`, then make **ONE**
   `config_update` call for the whole section (batched dotted keys — not one call
   per field). Section order:

   1. **identity** — `developer_name`, `project_name` (optional statusline label
      shown between open-tasks and MCP; empty falls back to the project folder name).
   2. **DAIC / workflow** — `protocol_engine.enabled`, `branch_enforcement.enabled`,
      `branch_enforcement.branch_prefixes`, `task_detection.enabled`, `blocked_tools`,
      `test_command`, `api_mode`.
   3. **auto-compact** — `auto_compact.enabled`, `auto_compact.threshold`,
      `auto_compact.context_limit` (optional model token budget; e.g. `1000000`
      for a 1M context window, `200000` for 200k — overrides auto-detection from
      the model name; must be a positive integer).
   4. **issue-tracking** — `issue_tracking.provider` (gitlab/jira/github/disabled),
      `issue_tracking.auto_sync`, then the chosen provider's NON-secret keys
      (`<provider>.enabled`, `base_url`, `project_path` / `project_key` /
      `repository`, `auto_sync`, `issue_tracking_enabled`, labels). After this
      section, if a provider is enabled but its token is unset, give the
      **teammate-advisory**:
      > Add your `<provider>` token to `.claude/state/provider-tokens.json` (key:
      > `<provider>`) — a per-project, git-ignored file the AI cannot read. Each
      > developer keeps their own; it is not shared through `config.json`.
   5. **AI providers** — `ai_providers.enabled_providers`, the six
      `ai_providers.include_in_*` flags, `ai_providers.timeout`, `codex.enabled`,
      `agy.enabled`.
   6. **wiki** — `wiki.enabled`. If enabling and `wiki/` is absent, offer to seed
      the wiki structure (`index.md`, `log.md`, `schema.md`, `raw/README.md`; pages
      live in lazily-created `pages/<category>/` subdirectories, and `schema.md`
      carries the `## Categories` list).
   7. **code review & display** — `code_review.enforce_warnings` (boolean; when true,
      code-review warnings must be acknowledged before task completion),
      `features.icon_style` (`nerd_fonts` / `emoji` / `ascii` — the statusline icon set).
   8. **notifications** — `notifications.enabled`, `notifications.mode`
      (`per_step` / `off`), `notifications.prefix`, `notifications.channels.telegram.enabled`,
      `notifications.channels.telegram.chat_id`, `notifications.channels.telegram.ca_bundle`
      (optional — leave empty unless discovery reports a TLS failure; see step 5 below).
      The Telegram **bot token is a secret** —
      it is NOT set here; direct the user to put it in `.claude/state/provider-tokens.json`
      (key: `telegram`), same as the provider tokens.

      **Telegram chat-id discovery (don't make the user hand-copy a chat id).** When
      the user enables the Telegram channel and has set the bot token, offer to
      discover the chat id instead of typing it:
      1. Call `mcp__plugin_team-management_tm__notification_discover_telegram_chats`
         (no arguments — the bot token is read from the token file, never passed).
      2. **On `success: true` with a non-empty `chats` list:** present the chats
         (each `{id, type, title}`) via a single `AskUserQuestion` so the user picks
         one; the label should show the title + type, and you map the choice back to
         its `id`. Add a plain "enter the id manually" path via the auto-appended
         "Other" option.
      3. **Confirm before saving (Approach C):** call the same tool again with
         `test_chat_id: "<selected id>"` — it sends a one-line test message to that
         chat. Ask the user (free-text, not a widget) whether the message arrived. If
         yes, write `notifications.channels.telegram.chat_id` via `config_update`
         (with `notifications.enabled: true` and `notifications.channels.telegram.enabled: true`
         in the same batched call). If no, let them pick a different chat or enter the
         id manually.
      4. **Fallbacks — always be honest about the limitation:** Telegram has no
         endpoint that lists a bot's groups, so discovery only surfaces *recently-active*
         chats. If the tool returns `success: false` or an empty `chats` list, relay its
         `hint` (e.g. "message the bot / add it to the group and send a message first,
         then retry", or the webhook-409 case) and fall back to asking the user to enter
         `chat_id` manually. Never block the section on discovery — it is a convenience.
      5. **TLS-verification failure (not a token problem).** If the tool returns
         `success: false` with a `hint` about TLS / certificate verification, the Python
         running the MCP server cannot verify Telegram's certificate — commonly a
         python.org build whose CA store was never initialised (no proxy involved).
         Relay the hint verbatim: run the python.org *Install Certificates.command*, or
         set `notifications.channels.telegram.ca_bundle` (or the `SSL_CERT_FILE` /
         `REQUESTS_CA_BUNDLE` env var) to a CA bundle path (for a corporate proxy, one
         that includes the proxy root). The bot token is fine in this case — do NOT tell
         the user to re-check it.

4. **Batched write contract.** Each section's `config_update` takes a flat object
   of dotted keys, e.g.:
   ```
   config_update(updates={
     "issue_tracking.provider": "gitlab",
     "gitlab.enabled": true,
     "gitlab.base_url": "https://gitlab.com",
     "gitlab.project_path": "team/app"
   })
   ```
   `config_update` validates the schema, rejects any secret key, enforces
   https-only URLs (SEC-007), keeps `config.json` git-ignored, and preserves any
   pre-existing token values untouched.

   **Surface the gitignore result.** Each `config_update` response carries a
   `gitignore` field — `{status, path, covered_by}` where `status` is `added`,
   `already_covered`, or `unavailable`. Tell the user **once** (not per section):
   if any write returned `status: "added"`, note that `team-management/config.json`
   was added to the project `.gitignore` so their per-project settings stay out of
   version control. If `status: "unavailable"` (the project is not a git repo, or
   `.gitignore` could not be written), note that the guard could not auto-ignore
   `config.json` — once the project is under git, they should make sure
   `team-management/config.json` is in `.gitignore`. `already_covered` needs no
   mention.

5. **If `config_update` is refused as "gated"**, the session window expired — tell
   the user to run `/team-management:config` again (the hook re-opens the gate).
   Never try to write the flag yourself; you cannot, and you should not.

6. **Confirm.** After the sections the user wanted, call `config_get` once more and
   show the resulting (masked) config so they can verify.

## Rules

- Never write tokens/secrets through `config_update` — they go in
  `.claude/state/provider-tokens.json` (per-project, git-ignored, AI-unreadable).
- One `AskUserQuestion` at a time; one `config_update` per section (batched).
- Only touch sections the user opts into — leave the rest unchanged.
- `config.json` must stay git-ignored; `config_update` enforces this and refuses to
  write a tracked file.
