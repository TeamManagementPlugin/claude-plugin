# MCP Server for Issue Tracking Integration

## Overview

The team-management MCP (Model Context Protocol) server exposes issue tracking operations (GitLab, Jira, GitHub / Gitea) as first-class tools that appear directly in Claude's tool list. This provides guaranteed discoverability and standardized integration with external systems.

## Why MCP?

**Problem**: Claude agents need reliable discovery of GitLab/Jira/GitHub/Gitea integration capabilities. Embedded Python utilities aren't visible in the tool list.

**Solution**: MCP tools appear in Claude's native tool list with full descriptions, making them immediately discoverable and accessible to all agents.

## Benefits

1. **Guaranteed Discovery**: Tools appear in Claude's tool list automatically
2. **Standardization**: MCP is Anthropic's official standard for AI-data integration
3. **Separation of Concerns**: Sessions logic in hooks, integration as tooling
4. **Multi-Provider Support**: Supports GitLab, Jira, and GitHub / Gitea with unified interface

## Installation

The MCP server is **bundled with the team-management plugin** — there is nothing to install
separately. When the plugin loads, `plugin/.mcp.json` registers the server, and a
cold-start bootstrap (`plugin/mcp/bootstrap_mcp.py`) builds an isolated venv (including
`mcp`, the official MCP SDK) under Claude Code's managed plugin-data directory before the server starts. See
[INSTALL.md](INSTALL.md) for installing the plugin itself.

## Configuration

No manual `.mcp.json` editing is required — the plugin registers and launches the server.
Its tools appear under the `mcp__plugin_team-management_tm__*` namespace once the cold-start
build finishes (usually the turn after first load). The server reads
`team-management/config.json` for the active issue-tracking provider; set non-secret config
via `/team-management:config` and put provider tokens in the per-project, git-ignored
`.claude/state/provider-tokens.json` (keyed by provider name; owner-only and unreadable by
Claude, so tokens never enter `config.json`).

## Available Tools

The MCP server exposes **42 tools across 8 modules** (issue tracking, protocol, code
review, git/MR, DAIC, notifications, release, config). The six issue-tracking tools
below are documented in detail as representative examples; for the complete,
drift-guarded inventory see `plugin/mcp/CLAUDE.md` ("MCP Tools (42 total)") and
`test/test_mcp_tool_inventory.py`.

### 1. `issue_status`

Show active issue tracking provider status and configuration.

**Inputs**: None

**Returns**:
- Provider name (GitLab, Jira, GitHub, or disabled)
- Linked tasks count
- Provider-specific configuration details
- List of linked tasks with issue IDs

**Example (GitLab)**:
```json
{
  "success": true,
  "provider": "GITLAB",
  "linked_tasks": 3,
  "base_url": "https://gitlab.com",
  "project_path": "namespace/project",
  "tasks": [
    {
      "task": "h-implement-feature",
      "issue_id": "gitlab:42",
      "issue_number": 42,
      "last_synced": "2025-10-08T14:30:00"
    }
  ]
}
```

**Example (GitHub)**:
```json
{
  "success": true,
  "provider": "GITHUB",
  "linked_tasks": 2,
  "base_url": "https://api.github.com",
  "repository": "owner/repo",
  "workflow_labels": {
    "in_progress": "in-progress",
    "blocked": "blocked",
    "pending": "pending"
  },
  "tasks": [
    {
      "task": "m-fix-bug",
      "issue_id": "github:123",
      "issue_number": "123",
      "last_synced": "2025-11-04T10:30:00"
    }
  ]
}
```

### 2. `issue_read`

Import an issue as a Claude task.

**Inputs**:
- `issue_id_or_url` (string): Issue ID or full URL
  - GitLab: `"123"` or `"https://gitlab.com/namespace/project/-/issues/123"`
  - Jira: `"PROJ-123"` or `"https://jira.company.com/browse/PROJ-123"`
  - GitHub: `"123"` or `"https://github.com/owner/repo/issues/123"`

**Returns**:
- Task name
- Task file path
- Provider-prefixed issue ID
- Next steps for working on the task

**Example**:
```json
{
  "success": true,
  "provider": "GITLAB",
  "task_name": "m-fix-authentication-bug",
  "task_file": "team-management/tasks/m-fix-authentication-bug.md",
  "issue_id": "gitlab:42",
  "next_steps": [
    "Update .claude/state/current_task.json to switch to this task",
    "Create branch: git checkout -b fix/m-fix-authentication-bug"
  ]
}
```

### 3. `issue_create`

Create an issue from a Claude task.

**Inputs**:
- `task_name` (string): Name of the task (without path or `.md` extension)

**Returns**:
- Provider-prefixed issue ID
- Task name
- Link status

**Example**:
```json
{
  "success": true,
  "provider": "JIRA",
  "task_name": "h-implement-feature",
  "issue_id": "jira:PROJ-456",
  "linked": true
}
```

### 4. `issue_sync`

Sync task status to linked issue.

**Inputs**:
- `task_name` (string): Name of the task to sync

**Returns**:
- Sync status
- Current task status
- Provider confirmation

**Example**:
```json
{
  "success": true,
  "provider": "GITLAB",
  "task_name": "h-implement-feature",
  "status": "in-progress",
  "synced": true
}
```

### 5. `issue_link`

Link an existing task to an issue.

**Inputs**:
- `task_name` (string): Name of the task
- `issue_id` (string): Issue ID
  - GitLab: numeric (e.g., `"42"`)
  - Jira: issue key (e.g., `"PROJ-123"`)

**Returns**:
- Provider-prefixed issue ID
- Link status

**Example**:
```json
{
  "success": true,
  "provider": "GITLAB",
  "task_name": "m-refactor-auth",
  "issue_id": "gitlab:42",
  "linked": true
}
```

### 6. `issue_unlink`

Remove issue link from a task.

**Inputs**:
- `task_name` (string): Name of the task to unlink

**Returns**:
- Unlink status

**Example**:
```json
{
  "success": true,
  "provider": "GITLAB",
  "task_name": "m-refactor-auth",
  "unlinked": true
}
```

## Provider Configuration

The MCP server automatically detects the active provider from `team-management/config.json`.

### GitLab Configuration

```json
{
  "issue_tracking": {
    "provider": "gitlab"
  },
  "gitlab": {
    "enabled": true,
    "api_token": "glpat-xxxxxxxxxxxx",
    "base_url": "https://gitlab.com",
    "project_path": "namespace/project",
    "auto_sync": true,
    "default_labels": ["claude-code", "automated"]
  }
}
```

### Jira Configuration

```json
{
  "issue_tracking": {
    "provider": "jira"
  },
  "jira": {
    "enabled": true,
    "api_token": "your-jira-token",
    "base_url": "https://jira.company.com",
    "project_key": "PROJ",
    "default_issue_type": "Task",
    "auto_sync": true
  }
}
```

### GitHub / Gitea Configuration

```json
{
  "issue_tracking": {
    "provider": "github"
  },
  "github": {
    "enabled": true,
    "api_token": "ghp_xxxxxxxxxxxx",
    "base_url": "https://api.github.com",
    "repository": "owner/repo",
    "auto_sync": true,
    "workflow_labels": {
      "in_progress": "in-progress",
      "blocked": "blocked",
      "pending": "pending"
    }
  }
}
```

**For Gitea** (self-hosted GitHub-compatible):
```json
{
  "issue_tracking": {
    "provider": "github"
  },
  "github": {
    "enabled": true,
    "api_token": "your-gitea-access-token",
    "base_url": "https://git.example.com/api/v1",
    "repository": "team/project",
    "auto_sync": true,
    "workflow_labels": {
      "in_progress": "in-progress",
      "blocked": "blocked",
      "pending": "pending"
    }
  }
}
```

**Note**: Gitea uses the GitHub-compatible API, so configuration uses the `github` provider with Gitea-specific `base_url` (ending in `/api/v1`).

### Disabling Provider

```json
{
  "issue_tracking": {
    "provider": "disabled"
  }
}
```

## Usage Examples

### Import a GitLab Issue

```
Claude, use issue_read with issue_id_or_url: "https://gitlab.com/namespace/project/-/issues/42"
```

or

```
Claude, use issue_read with issue_id_or_url: "42"
```

### Create Jira Issue from Task

```
Claude, use issue_create with task_name: "h-implement-feature"
```

### Check Integration Status

```
Claude, use issue_status
```

### Sync Task Status

```
Claude, use issue_sync with task_name: "h-implement-feature"
```

## MCP Tool Interface

All team-management functionality is exposed through `mcp__plugin_team-management_tm__issue_*` tools — first-class tools integrated with Claude Code's tool system.

### Why MCP Tools

1. **First-Class Integration**: MCP tools are native to Claude Code's tool system
2. **Automatic Discovery**: Always visible in tool list, no documentation lookup needed
3. **Better Error Handling**: Structured JSON responses with clear error messages
4. **Type Safety**: Parameters are validated by MCP framework
5. **Performance**: Direct tool calls without shell command expansion
6. **Consistency**: Same interface pattern as other Claude Code tools

### Example Usage

```
use mcp__plugin_team-management_tm__issue_create(task_name="m-fix-bug")
```

## Troubleshooting

### MCP Server Not Starting

**Problem**: Tools don't appear in Claude's tool list.

**Solutions**:
1. On first load the server **cold-starts** — it builds its private venv under Claude Code's managed plugin-data directory before exposing tools. Let the first session finish and retry; the tools appear once the build completes (usually the next turn).
2. Confirm the plugin is enabled (`/plugin`) so `plugin/.mcp.json` registers the server.
3. Restart Claude Code after enabling or updating the plugin.
4. Check Claude Code logs for MCP server / cold-start errors.

### Provider Not Detected

**Problem**: Tools return "No issue tracking provider enabled" error.

**Solutions**:
1. Check `team-management/config.json` has provider enabled
2. Verify `issue_tracking.provider` is set to `"gitlab"`, `"jira"`, or `"github"` (GitHub / Gitea)
3. Ensure provider configuration (api_token, base_url, etc.) is complete
4. Test provider connection with `mcp__plugin_team-management_tm__issue_status` tool

### Import Errors

**Problem**: `ModuleNotFoundError: No module named 'mcp'`

**Solutions**:
1. The plugin builds its own venv (including `mcp`, the official MCP SDK that provides `mcp.server.fastmcp`) on cold start — let the first session finish building, then retry; if it never completes, fully restart Claude Code.
2. Confirm the plugin is enabled (`/plugin`) so the cold-start bootstrap runs.

### Connection Errors

**Problem**: "Could not connect to GitLab/Jira" errors.

**Solutions**:
1. Verify API token is valid and not expired
2. Check base_url is correct and accessible
3. For GitLab: Verify project_path format is `namespace/project`
4. For Jira: Verify project_key exists and is accessible
5. Test connection manually with provider utilities

## Architecture

The server is a modular FastMCP app: a thin `server.py` entry point plus `core/`
(config loading + provider detection, project-root resolution), `helpers/`
(code-review utilities), and `tools/` (the tool modules — issue tracking, protocol,
code review, git, DAIC, notifications, release, config). It imports the provider utilities
(`gitlab_utils` / `jira_utils` / `github_utils`) directly from the plugin's `hooks/`
directory — a single source of truth, no copies.

**The maintained, detailed architecture reference is
[`plugin/mcp/CLAUDE.md`](../plugin/mcp/CLAUDE.md)** — full module map, the complete tool
inventory, the cold-start bootstrap, and the three-root path model.

### Provider routing

```
MCP tool call
    ↓
provider detection (team-management/config.json → issue_tracking.provider)
    ↓
├→ plugin/hooks/gitlab_utils.py   (GitLab)
├→ plugin/hooks/jira_utils.py     (Jira)
└→ plugin/hooks/github_utils.py   (GitHub / Gitea)
    ↓
provider API → response to Claude
```

### State

- Reads configuration from `team-management/config.json`.
- Maintains task-issue mappings in `.claude/state/`: `gitlab-mappings.json`,
  `jira-mappings.json`, `github-mappings.json`.
- Otherwise stateless.

## Development

Run the test suite from the repo root: `python3 -m pytest test/`. The MCP layer is
covered by `test/test_config_mcp.py`, `test/test_bootstrap_mcp.py`, and the
`test/test_mcp_*` modules (tool inventory, namespace, git-review tooling). To exercise
the server live, load the plugin with `claude --plugin-dir ./plugin` (see
[INSTALL.md](INSTALL.md) §4).

### Adding a new provider

To add a provider beyond GitLab / Jira / GitHub / Gitea (e.g. Linear):

1. Add a provider utility class under `plugin/hooks/` implementing the
   `IssueTrackingProvider` / `IssueTrackingTaskSync` interfaces.
2. Wire detection and routing in `plugin/mcp/core/config.py` (`detect_provider`).
3. Add the configuration schema to `plugin/templates/config.template.json`.
4. Update documentation.

## Security Considerations

- MCP server runs in user's Python environment (same as hooks)
- Reads same configuration files as existing tools
- No new authentication mechanisms introduced
- API tokens never logged or exposed in error messages
- Respects existing security patterns and permissions

## Performance

- **Startup**: Minimal overhead, fast server initialization
- **Tool Execution**: Direct delegation to provider utilities
- **Memory**: Stateless design, no caching or long-running state
- **Network**: Only when making API calls to providers

## Related Documentation

- [CLAUDE.md](../CLAUDE.md) - Project architecture overview
- [USAGE_GUIDE.md](./USAGE_GUIDE.md) - General usage documentation
- [INSTALL.md](./INSTALL.md) - Installation guide
- [hooks/CLAUDE.md](../plugin/hooks/CLAUDE.md) - Hooks module documentation
- [MCP Documentation](https://modelcontextprotocol.io) - Official MCP specifications

## Future Enhancements

Potential future additions:

1. **Real-time updates**: MCP resources for live issue status
2. **Batch operations**: bulk import/export of issues
3. **Webhooks**: automatic task updates from provider webhooks
4. **Linear**: support for Linear issue tracking
5. **Custom providers**: plugin system for custom issue trackers