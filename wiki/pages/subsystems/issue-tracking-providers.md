---
title: Issue Tracking Providers
tags: [issue-tracking, architecture, mcp]
created: 2026-05-31
updated: 2026-07-14
sources: [plugin/hooks/issue_provider_base.py, plugin/hooks/gitlab/api.py, plugin/hooks/gitlab/task_sync.py, plugin/hooks/github/api.py, plugin/hooks/github/task_sync.py, plugin/hooks/jira_utils.py, plugin/hooks/protocol_engine.py, plugin/mcp/tools/issue_tracking.py, plugin/mcp/core/config.py]
---

# Issue Tracking Providers

The multi-provider issue-tracking layer maps Claude tasks to external tracker issues (GitLab, GitHub/Gitea, Jira) and synchronizes them bidirectionally. It exists so the task lifecycle (create → in-progress → completed) and the work-log/code-review artefacts can be mirrored into whatever tracker a project already uses, without the protocol engine or MCP tools knowing provider specifics. Every provider implements the same two abstract interfaces, so callers (hooks, MCP tools, completion-dispatch funcs) program against `IssueTrackingProvider` / `IssueTrackingTaskSync`, never against a concrete API class.

This layer is the **single source of truth** for provider utilities: both the hooks (direct import) and the MCP server (`sys.path` bootstrap into `.claude/hooks/`) import the *same* modules. There is no copy in `mcp/`. See [Hooks System](pages/subsystems/hooks-system.md) for the hook side and [MCP Server](pages/subsystems/mcp-server.md) for the import bootstrap.

## The Abstraction (`issue_provider_base.py`)

Two ABCs (`issue_provider_base.py`):

- **`IssueTrackingProvider`** — the raw API surface. `__init__` calls `_find_project_root()` (env-first `CLAUDE_PROJECT_DIR`, then walks up for `.git`/`team-management`/`.claude` markers) and `_load_config()` (reads `team-management/config.json`). Abstract methods every provider must implement: `provider_name`, `get_issue`, `create_issue`, `update_issue`, `add_comment`, `parse_issue_url`, `format_issue_id`, `extract_issue_id`, `get_supported_issue_types`, `validate_configuration`. It also carries the **shared request layer** every provider uses: `_make_request` (the single HTTP method — GET/POST/PUT/PATCH/DELETE, 204→`{'success': True}`, `requests`-missing guard — built from each subclass's `self.api_base` / `self.headers` / `self.request_timeout`, with `BCC_HTTP_TIMEOUT_SECONDS` overriding the timeout), `_raise_on_error` (the uniform error-dict→`Exception` raise), and `_redact_token`. A provider's `__init__` differs only in auth header, base URL, and default timeout — the request/error machinery is not reimplemented per provider. It also carries `_warn_if_insecure_base_url`, called by all three provider `__init__`s after they set `self.base_url`: it **warns** (stderr + `get_provider_logger('<provider>.log')`), never rejects, when `base_url` is non-https and the host is not loopback (localhost / whole `127.0.0.0/8` / `::1`, via `ipaddress.is_loopback`) — a non-https base_url sends the API token in a cleartext header. It is best-effort (never raises) and builds a **credential-safe display** (prefers `urlparse`'s `hostname` → `scheme://host[:port]`, dropping userinfo/path/query/fragment; scheme-less fallback strips everything after the last `@`) so the warning itself cannot leak a credential embedded in the URL. This is the construction-time counterpart to the SEC-007 write-path guard in `mcp/tools/config.py::_validate_safe_url` (which only runs when a base_url is *written*), covering a legacy / hand-edited `config.json`.
- **`IssueTrackingTaskSync`** — the task↔issue bridge. `__init__` computes the per-provider mappings file as `.claude/state/{provider.provider_name}-mappings.json` and seeds it empty if missing via an atomic create-only `open('x')` (`_ensure_mappings_file`, race-safe against a concurrent writer). Abstract: `link_task_to_issue`, `import_issue_as_task`, `create_issue_from_task`, `sync_task_status_to_issue`. Concrete shared helpers: `get_task_mapping`, `unlink_task`, plus the work-log parsers below.

**Provider-prefixed IDs** are the cross-provider lingua franca: `gitlab:456`, `github:123`, `jira:PROJ-12`. `format_issue_id` / `extract_issue_id` add and strip the prefix; MCP tools strip it with a plain `issue_id.split(':', 1)[1]` (`issue_tracking.py`,).

### Shared work-log machinery (used by all three providers)

These live on `IssueTrackingTaskSync` so providers don't reimplement them:

- `find_task_file` (module-level) — resolves a task name to a file, searching `team-management/tasks/{name}.md`, then `tasks/done/`, then recursive `rglob`.
- `_collect_agent_summaries` — string-matches the `## Work Log` section for agent names (`code-review`, `service-documentation`, `testing`, `build`, …) to build a QA summary dict. This is heuristic substring matching, not structured parsing.
- `_generate_rich_completion_comment` — assembles the markdown comment posted on issue closure (problem statement, completed `- [x]` success criteria, agent QA section, MR/PR reference). Note it branches on `provider_name` to pick "Merge request !N" (GitLab) vs "Pull request #N" (others).
- `_extract_code_review_results` — pulls the **last** `# Code Review:` block from the work log via the regex `r'# Code Review:.*?(?=\n#[^#]|\Z)'` (DOTALL, non-greedy). A 1 MB input cap guards against ReDoS. "Last" matters: it grabs the final passing review after fixes, not the first.
- `_slugify` / `_extract_task_title` — the canonical task-name slugifier and the first-`# `-line title scan, single-sourced so the three `import_issue_as_task` / `create_issue_from_task` paths don't diverge. `_slugify` is **Unicode-aware** (`[\W_]+`→`-`): it keeps Cyrillic / CJK / accented letters (an ASCII-only regex would collapse such a title to an empty slug) and falls back to `"task"` for an all-punctuation title.

## Per-Provider Mechanics

### GitLab (`gitlab/api.py`, `gitlab/task_sync.py`)

- Auth header is `PRIVATE-TOKEN` (`api.py`); API base is `{base_url}/api/v4`.
- `project_path` is URL-encoded with `quote(..., safe='')` so `namespace/project` becomes `namespace%2Fproject` for path-segment use (`api.py`); the raw form is kept in `raw_project_path`.
- **Default-branch auto-detection** `_detect_default_branch` (`api.py`) is a 3-strategy cascade: (1) GitLab project API `default_branch`, (2) git `symbolic-ref refs/remotes/origin/HEAD`, (3) scan `git branch -a` for `origin/main` then `origin/master`, falling back to `'master'`. `default_branch` is a **lazy `@property`** — detected on first access and cached in `_cached_default_branch`, with a setter that pins it — so constructing `GitLabAPI()` makes no network call (the detection used to run eagerly in `__init__`).
- **IID vs ID**: GitLab issues have a global `id` and a project-scoped `iid`. Mappings store both (`gitlab_issue_id`, `gitlab_issue_iid`, `task_sync.py`). **Everything user-facing and every REST call uses the `iid`** — `add_issue_comment` posts to `/issues/{iid}/notes` (`api.py`). As of m-fix-issue-tracking-integration-robustness both *producers* surface the iid: `create_gitlab_issue_from_task` returns `issue['iid']` (`task_sync.py`, was the global `id`) and MCP `issue_status` keys the `gitlab:<n>` display, `issue_number`, and the MR-only skip guard on `gitlab_issue_iid`. The global `gitlab_issue_id` is still stored in the mapping but is no longer surfaced as a handle — surfacing it round-tripped a global id into the iid-based path and 404'd `issue_set_status`/`issue_comment`.
- URL parser `parse_gitlab_issue_url` (`gitlab/__init__.py`) matches the `/-/issues/N` GitLab path shape and returns `{base_url, project_path, issue_id}`.
- Status map (`task_sync.py`): `completed→close`, `in-progress/pending→reopen`, `blocked→None` (no state change). Applied via `update_issue(state_event=...)`.

### GitHub / Gitea (`github/api.py`, `github/task_sync.py`)

GitHub and Gitea share one provider because Gitea exposes a GitHub-compatible v3 API. The differences are handled by a single runtime flag.

- **Gitea auto-detection**: `is_gitea` property (`api.py`) returns true when the base URL contains `/api/v1` or the literal `gitea`. GitHub default base is `https://api.github.com`; Gitea is `https://gitea.example.com/api/v1`.
- Auth header is `token <pat>` with `Accept: application/vnd.github.v3+json` (`api.py`); request timeout is env-tunable via `BCC_HTTP_TIMEOUT_SECONDS` (default 15s, `api.py`).
- **Labels: names vs IDs — the central Gitea quirk.** GitHub accepts label *names* directly (`issue_data['labels'] = label_names`, `api.py`). Gitea requires label *IDs*. `_convert_labels_for_gitea` (`api.py`) fetches the repo label set (`_get_label_name_to_id_map`, cached on `self._label_cache`, `api.py`), maps names→IDs, and **auto-creates** any missing label via `_create_label` (`api.py`, color default `#0052CC`). On create the payload key is capitalized `'Labels'` (`api.py`) and updates go through a dedicated `PUT /issues/{id}/labels` endpoint (`_set_issue_labels_gitea`, `api.py`) rather than the issue PATCH body. This is why `get_provider_sync`/`get_provider_api` use a **singleton** for GitHub (`mcp/core/config.py`) — to keep the label cache warm and avoid duplicate label creation across tool calls.
- **Frontmatter stripping**: `_strip_yaml_frontmatter` (`task_sync.py`) removes a leading `---\n...\n---\n` block from the issue body on import. Gitea issues created by team-management carry the task's YAML frontmatter in their body; without stripping, importing such an issue would nest frontmatter inside the new task's `## Problem/Goal`.
- Status map (`task_sync.py`): `completed→closed`, `in-progress/pending→open`, `blocked→None`. Plus a workflow label is swapped in via `workflow_labels` config, filtering out other workflow labels first so only one is active at a time (`task_sync.py`).
- `_make_request` returns `{'success': True}` on HTTP 204 (`api.py`) since DELETE/empty responses have no JSON body.

### Jira (`jira_utils.py`, single-file)

- Auth is **Bearer PAT** (`Authorization: Bearer <pat>`, `jira_utils.py`) — built for self-hosted Jira. API base is `{base_url}/rest/api/{version}`, version 2 or 3.
- **Markdown ↔ Jira-Wiki conversion** is the Jira-specific quirk. For API v2, descriptions/comments are converted from Markdown to Jira Wiki Markup by `_markdown_to_jira_wiki`; for v3 they're wrapped in the Atlassian Document Format (ADF) JSON tree (`_format_description`). The converter uses a **placeholder protection** scheme: it first extracts code blocks/inline code into `__CODE_BLOCK_N__` placeholders, runs the formatting regexes (headers `#`→`h1.`, bold `**`→`*` via a `__BOLD_START__` shuttle to avoid italic interference, `[text](url)`→`[text|url]`, etc.), then restores the code last. Checkboxes degrade to `* (x)` / `* ( )` since Jira Wiki has no native checkbox.
- On import, Jira's ADF body is flattened back to plain text by walking the `content` tree for `text` nodes.
- Status transitions are not direct state writes: `update_issue` with a `status` first GETs `/issue/{id}/transitions` and POSTs the matching transition id. Status map: `completed→Done`, `blocked→Blocked`, `in-progress→In Progress`, `pending→To Do`.

## Issue ⇄ Task Flow

**Import (issue → task)** — `import_issue_as_task`: fetch the issue, slugify its title into `{priority}-{slug}` (truncated to ~50 chars), write a task file under `team-management/tasks/` with standard sections (`## Problem/Goal`, `## Success Criteria`, provider-detail block, `## Work Log`), then `link_task_to_issue` to record the mapping. All three providers accept `update_mode=True` to re-fetch into an existing file instead of erroring — the MCP `issue_read` tool passes it uniformly to every provider (GitLab/Jira gained the parameter so that call no longer `TypeError`s).

**Create (task → issue)** — `create_issue_from_task` (GitLab `create_gitlab_issue_from_task`, `task_sync.py`): parse the task title (first `# ` line), send the whole task body as the issue description plus a generated footer, persist the mapping. Jira additionally infers issue type from the task name (`bug`/`fix`→Bug, `epic`→Epic, `story`→Story, `jira_utils.py`).

**Status sync** — `sync_task_status_to_issue` (reached via the MCP `issue_sync` tool, which parses `status:` from frontmatter at `issue_tracking.py`): applies the provider status map, posts a comment (rich completion comment for `completed`, plain text otherwise), and refreshes `last_synced`.

### Mapping files & sync metadata

One JSON per provider in `.claude/state/`: `gitlab-mappings.json`, `github-mappings.json`, `jira-mappings.json`. Keyed by task name. A mapping carries issue identifiers plus sync metadata (`last_synced`, `sync_count`, `description_updated`) and, after MR/PR creation, `merge_request_iid`/`pull_request_number` etc.

Every mutation is a read-modify-write, and these files are written from two processes (the PostToolUse auto-sync hook and the long-lived MCP server), so all 14 mutation sites route through `IssueTrackingTaskSync._locked_mapping_update(mutator)` — the RMW runs under the shared `_state_lock` with a two-marker best-effort degrade (m-fix-mappings-lock-free-rmw). See [State Files](pages/entities/state-files.md) for the locking + degrade model. Historically these were unlocked and a concurrent RMW silently lost one writer's update.

**MR/PR-only mappings (gotcha-adjacent design)**: when `issue_tracking_enabled` is false or a task was never linked, `create_merge_request_for_task` / `create_pull_request_from_task` still persist a mapping containing *only* MR/PR fields and `last_synced` — no `*_issue_id`. This makes the mapping truthy-but-issueless. Consumers that subscript `mapping['issue_id']` would `KeyError`; the codebase uses the guarded idiom `issue_id = mapping.get('issue_id') if mapping else None; if not issue_id: <skip>` (e.g. `github/task_sync.py`, `gitlab/task_sync.py`/). The MCP `issue_status` tool `continue`s past issueless mappings so its `tasks` list stays "issue-linked only", recomputing `linked_tasks` after the loop (`issue_tracking.py`,).

## Provider Routing & MCP Surface

`detect_provider` (`mcp/core/config.py`) reads `issue_tracking.provider`; if it's not `"disabled"` and that provider's `.enabled` is true, that wins. Otherwise it falls back to scanning individual `gitlab/jira/github.enabled` flags. The result is cached in a module global. `get_provider_api` / `get_provider_sync` (/) instantiate the concrete classes — singletons for GitHub only. The hooks-layer factories `get_{gitlab,github,jira}_sync` share one contract — **return None + log on failure** (not raise) — so every caller null-checks uniformly across providers.

The MCP tools in `tools/issue_tracking.py` (14 tools) are thin provider-agnostic wrappers: each calls `detect_provider()` + `get_provider_sync()`/`get_provider_api()` and branches on the provider string only where the API genuinely differs (e.g. `issue_set_status` maps `closed/close` per provider). The escape hatch `issue_api` makes a raw `_make_request` call for operations no tool covers — it validates method and rejects `..` path traversal. `issue_dependency` is Gitea-full / GitHub-same-repo-only / GitLab-unsupported. MR/PR *creation* lives in separate tool modules — see [Completion and Git Flow](pages/procedures/completion-and-git-flow.md).

## Structured Error Reporting

The shared `_make_request` on `IssueTrackingProvider` (single-sourced in `issue_provider_base.py`) catches `requests` exceptions and returns a uniform **error dict** instead of raising:

```
{error: True, error_type, status_code, message, response_body, url, method}
```

`error_type` is one of `http_error` / `timeout` / `connection_error` / `request_error` / `unexpected_error`; `response_body` is truncated to the first 500 chars (the token is redacted first). The public API methods and the MR/PR/release managers funnel the result through the shared `_raise_on_error` helper, which raises an `Exception` folding in status code and response body when `result.get('error')` is set — so callers get a debuggable failure without the raw transport exception. The MCP `issue_api` tool surfaces the dict fields directly (`issue_tracking.py`). Tokens are never echoed into error messages.

## Gotchas

- **GitLab uses `iid` for everything user-facing; the global `id` is stored but never surfaced.** Comments, updates, MR linkage, the `create_*` return value, and `issue_status` display all key on `iid`; the mapping keeps `gitlab_issue_id` for reference only. Surfacing the global `id` as a round-trip handle silently 404s the iid-based REST path — the bug fixed in m-fix-issue-tracking-integration-robustness at both producers (`create_gitlab_issue_from_task` + `issue_status`).
- **`create_issue_from_task` is not idempotent on its own — the protocol-engine wrapper guards it.** Calling it twice creates a second issue and overwrites the mapping (orphaning the first). The protocol engine's `_func_create_issue_if_enabled` (the `investigation` post_func, `protocol_engine.py`) now checks `get_task_mapping` for the provider's issue-id key (gitlab→`gitlab_issue_iid`, github→`issue_id`, jira→`jira_issue_key`/`jira_issue_id`) before creating and returns `action="skipped"` when already linked — so a re-run of the investigation step (dirty-tree pause, `protocol_goto` back) no longer duplicates (m-fix-issue-tracking-integration-robustness). MR/PR-only mappings lack the issue-id key, so they are correctly treated as *not* linked.
- **Gitea label payload casing is asymmetric.** Create uses `'Labels'` (capital, IDs); GitHub create uses `'labels'` (lowercase, names). Update on Gitea bypasses the issue PATCH entirely (`PUT .../labels`). Forgetting the singleton would re-fetch/recreate labels every call.
- **Gitea auto-creates missing labels** as a side effect of any create/update that references an unknown name — there is no "label not found" error path; it just appears in the repo.
- **`_collect_agent_summaries` is substring matching, not structured.** A work-log line merely *containing* "test" produces a `testing` QA entry. Reliable signal depends on the logging agent's formatting conventions.
- **`_extract_code_review_results` returns the LAST review block.** If multiple `# Code Review:` headings exist (review → fix → re-review), only the final one is posted to the MR/PR. Intentional, but surprising if you expect all of them.
- **Jira status changes can silently no-op.** If no workflow transition matches the target status name (case-insensitive), the transition loop simply finds nothing and the issue stays put — `update_issue` does not error on a missing transition (`jira_utils.py`).
- **Jira markdown converter is regex-based.** Nested formatting, tables, or unusual code-fence content can convert imperfectly; the placeholder scheme only protects fenced/inline code, nothing else.
- **The MCP layer's frontmatter status parse is its own mini-parser** (`issue_tracking.py`), separate from `shared_state.parse_task_frontmatter` used by hooks — two parsers for the same field. Keep them in mind if status detection diverges between auto-sync and `issue_sync`.
- **Truthy-but-issueless mappings are real and reachable.** Any new consumer of `get_task_mapping()` must use the `.get('issue_id')` guard idiom, or it will `KeyError` on MR/PR-only tasks (this exact bug was fixed across 5 GitHub + 3 GitLab call sites).
- **`issue_push` is unimplemented for Jira** — it returns an explicit error pointing at `issue_sync` (`issue_tracking.py`).
