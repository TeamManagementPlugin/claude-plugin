# Agents Module CLAUDE.md

## Purpose
Provides specialized subagents for complex operations in team-management, including multi-provider issue tracking synchronization (GitLab, Jira, GitHub / Gitea), code review, and task management automation.

## Narrative Summary

The agents module implements the delegation pattern for team-management, providing specialized subagents that operate in separate context windows to handle complex, file-heavy operations. Each agent is designed for a specific domain of expertise and operates independently to avoid context pollution in the main conversation thread.

With the addition of multi-provider issue tracking integration, the agents module now includes sophisticated synchronization capabilities that handle bidirectional sync between Claude tasks and external issues (GitLab, Jira, GitHub / Gitea), including error recovery, conflict resolution, and batch operations across different providers.

## Module Structure

### Agent Files
- `context-gathering.md` - Task context manifest creation
- `context-refinement.md` - Session discovery integration
- `code-review.md` - Security and quality review; output template includes a `## ✨ Strengths (1-3)` section before Critical Issues (omit section entirely if nothing genuinely stands out — no performative praise)
- `spec-compliance-reviewer.md` - Read-only agent that audits the git diff (including untracked files via `git ls-files --others --exclude-standard`) against task Success Criteria. Returns PASS/FAIL verdict with per-criterion coverage breakdown and scope-creep flags. Output is structured so the orchestrator can write the `SPEC_REVIEW: PASSED` sentinel via `protocol_save_note`, which gates the `code-review` step's post_func `require_spec_review_passed`. Tools: Read, Grep, Glob, Bash.
- `code-cleanliness.md` - Code quality and maintainability analysis
- `code-explorer.md` - Existing codebase feature analysis and execution flow tracing
- `code-architect.md` - Architecture design and implementation blueprints
- `logging.md` - Work log consolidation and cleanup
- `service-documentation.md` - CLAUDE.md file maintenance
- `critic.md` - Devil's advocate analysis for proposals and brainstorm topics
- `user-perspective.md` - End-user impact and UX analysis
- `risk-security-analyst.md` - Combined risk, dependency, and security assessment
- `scope-strategist.md` - Scope definition and implementation phasing
- `codex-cli.md` - Pass-through wrapper agent that shells out to the `codex` CLI (`codex review --uncommitted` for review, `codex exec -s read-only` for exec); the caller owns the full prompt. Each `codex` invocation runs under an `env -i PATH HOME` scrub so plugin `userConfig` secrets are not inherited. Full contract (timeout fallback, sandbox, output shape) in the root `CLAUDE.md` "AI Provider Integration".
- `agy-cli.md` - Pass-through wrapper agent that shells out to the Google Antigravity (`agy`) CLI (`agy --add-dir "$PWD" --dangerously-skip-permissions --print-timeout 300s -p ...` — NO `--sandbox`, which breaks git on macOS) behind a 330s external watchdog. skip-permissions gets past the headless soft-deny and is CONTAINED by a project-local `.agents/hooks.json` read-only deny-gate (deployed by `shared_state.ensure_agy_readonly_gate_deployed` when agy is enabled); the wrapper preflight-verifies the gate and refuses to run uncontained. Snapshots `git status`/`git diff HEAD` before-and-after and prepends an `agy review WARNING: agy modified files...` line if agy mutated files (detect & report, never auto-revert). Runs under the same `env -i PATH HOME` scrub; failure surfaces as `agy review unavailable: <reason>`. Full contract (watchdog fallback, mutation-check details) in the root `CLAUDE.md` "AI Provider Integration".
- **Provider Extensibility**: Agent system supports future issue tracking provider agents

### Agent Architecture

Each agent follows the standard format:
```markdown
---
name: agent-name
description: Agent purpose and capabilities
tools: Allowed tools for agent execution
---

# Agent Implementation
Detailed instructions and operational procedures
```

**Source-managed banner.** Every shipped agent source under `plugin/agents/` (all `*.md` except this `CLAUDE.md`) carries an HTML-comment do-not-edit banner immediately under its frontmatter. It tells a human not to edit shipped agents in place — agents ship with the plugin and are replaced on plugin update. To customize an agent, copy it to a new name (e.g. `my-code-review.md`), which the plugin never overwrites. The drift-guard `test/test_agent_banner_drift.py` asserts the banner is present on every shipped agent.

## Feature Development Agents

### Code Explorer Agent (code-explorer.md)

#### Purpose and Scope
- Deep analysis of existing codebase features
- Execution flow tracing from entry points to data storage
- Architecture layer mapping and pattern identification
- Dependency documentation for informed development decisions
- Complements context-gathering agent (explorer=research phase, gathering=task preparation)

#### Core Capabilities
- **Feature Discovery**: Locate entry points, core files, boundaries, and configurations
- **Code Flow Tracing**: Follow call chains, trace data transformations, identify dependencies
- **Architecture Analysis**: Map abstraction layers, design patterns, cross-cutting concerns
- **Implementation Details**: Algorithms, error handling, performance considerations, technical debt

#### team-management Integration
- Reads CLAUDE.md files for architectural guidance
- Checks protocol files for workflow patterns
- Identifies hook integration points and agent delegation patterns
- Reviews related task files in team-management/tasks/

#### Usage Pattern
Typically launched in parallel (2-3 instances) during the codebase exploration phase:
- Agent 1: Trace similar feature implementations
- Agent 2: Map high-level architecture and abstractions
- Agent 3: Analyze specific subsystem or integration points

Each agent returns 5-10 essential files to read for deep understanding.

### Code Architect Agent (code-architect.md)

#### Purpose and Scope
- Architecture design for new features and enhancements
- Comprehensive implementation blueprints with specific file modifications
- Pattern-based design aligned with existing codebase conventions
- Decisive architectural recommendations with rationale

#### Core Capabilities
- **Pattern Analysis**: Extract existing patterns, conventions, architectural decisions from CLAUDE.md files
- **Architecture Design**: Make confident design choices optimized for testability, performance, maintainability
- **Implementation Blueprints**: Complete specifications including files to modify, component responsibilities, data flows, build sequences

#### team-management Integration
- Suggests task structure (priority prefix, branch strategy)
- Considers protocol integration and hook impacts
- Recommends agent delegation opportunities
- Includes issue tracking and testing strategies
- Plans documentation updates

#### Usage Pattern
Typically launched in parallel (2-3 instances) during the architecture design phase with different architectural focuses:
- **Minimal Changes**: Smallest change, maximum code reuse
- **Clean Architecture**: Maintainability-focused, elegant abstractions
- **Pragmatic Balance**: Speed + quality trade-off

Each agent provides one confident approach emphasizing its assigned focus. User selects preferred approach.

#### Output Format
- **Patterns Found**: Existing patterns with file:line references
- **Architecture Decision**: Chosen approach with rationale and trade-offs
- **Component Design**: File paths, responsibilities, dependencies, interfaces
- **Implementation Map**: Specific files to create/modify with detailed changes
- **Data Flow**: Complete flow from entry to output
- **Build Sequence**: Phased implementation checklist
- **Critical Details**: Error handling, state management, testing, security

## Brainstorm Analysis Agents

These agents are launched in parallel during the brainstorm protocol's analysis step (step 3). They provide multi-perspective evaluation of brainstorm topics. All are universal — usable outside the brainstorm protocol as well.

### Critic Agent (critic.md)

#### Purpose and Scope
- Devil's advocate analysis of proposals and features
- Identifies flaws, unnecessary complexity, and project risks
- Provides severity-rated concerns with counter-arguments and mitigations

#### Analysis Framework
- **Necessity Assessment** includes an explicit **YAGNI check**: flag scope designed for hypothetical future requirements rather than problems that exist today.
- **Complexity Analysis** includes an explicit **YAGNI check**: flag abstractions introduced before three concrete call sites justify them (three similar lines beat a premature abstraction).
- Severity levels: Critical / High / Medium / Low
- Output structure: what / why it matters / evidence / mitigation per concern

#### Usage Pattern
- Launched during brainstorm analysis step alongside other specialists
- Can also be invoked standalone: "Be the devil's advocate on this approach"
- subagent_type: `critic`

### User Perspective Agent (user-perspective.md)

#### Purpose and Scope
- End-user/system-user impact analysis
- Evaluates convenience, new capabilities, friction points
- Capability delta assessment (what opens up, what closes down)

#### Usage Pattern
- Launched during brainstorm analysis for UX impact evaluation
- Can be invoked for any feature evaluation: "What's the user impact of this change?"
- subagent_type: `user-perspective`

### Risk & Security Analyst Agent (risk-security-analyst.md)

#### Purpose and Scope
- Combined risk and security assessment
- External dependencies, failure modes, migration risks
- Attack vectors, vulnerabilities, data exposure, auth/authz gaps
- Structured risk matrices and threat models

#### Usage Pattern
- Launched during brainstorm analysis for comprehensive risk/security evaluation
- Can be invoked for security reviews: "Assess the security implications of this design"
- subagent_type: `risk-security-analyst`

### Scope Strategist Agent (scope-strategist.md)

#### Purpose and Scope
- Full implementation scope definition (not MVP)
- Natural phase identification and execution ordering
- Task dependency analysis and parallelism opportunities

#### Usage Pattern
- Launched during brainstorm analysis for scope and phasing strategy
- Can be invoked for planning: "Break this feature into implementation phases"
- subagent_type: `scope-strategist`

## Agent Execution Model

### Context Isolation
- Each agent operates in separate context window
- No context pollution in main conversation
- Independent tool access and execution
- Results returned to main thread

### State Coordination
- Agents access shared state files
- Coordination through file-based state management
- No direct agent-to-agent communication
- Main thread orchestrates agent results

### Tool Restrictions
- Each agent has specific allowed tools
- Security constraints prevent privilege escalation
- Sandboxed execution environment

## Integration with Main System

### Task Agent Integration
- Agents called via Task tool from main conversation
- Lightweight prompting with context from session history
- Results integrated into main workflow
- Agent state isolated from main conversation state

### Subagent Detection (shared across agents)
- A file-locked depth counter at `.claude/state/subagent-depth.json` prevents hook interference
- `task-transcript-link.py` increments the counter on Task/Agent PreToolUse (before agent execution); `post-tool-use.py` decrements it on Task/Agent PostToolUse (clamped at 0). Both hooks recognise BOTH tool names — some Claude Code harnesses name the subagent-dispatch tool `Agent` instead of `Task` (`task-transcript-link.py` gates on `tool_name not in ("Task", "Agent")`; `post-tool-use.py` decrements on `tool_name in ("Task", "Agent")`). When only `Task` was recognised, an `Agent` dispatch never incremented the counter, so `in_subagent_context()` always returned False inside such subagents — silently defeating the auto-worklog gate, the DAIC subagent bypass, reminder/auto-sync suppression, and transcript staging. The PreToolUse matcher for `task-transcript-link.py` in `plugin/hooks/hooks.json` is `Task|Agent` to match.
- `in_subagent = shared_state.in_subagent_context()` (depth > 0); agents operate without DAIC enforcement while depth > 0
- Counting (not a boolean) keeps parallel subagents protected — the first to finish decrements N→N-1, not to 0, so still-running siblings stay detected
- Hard-reset to 0 on `UserPromptSubmit` (user-messages.py) and `SessionStart` (session-start.py) self-heals any leak from a hard-interrupted or denied Task; session-start also removes the legacy `in_subagent_context.flag`
- **Transcript staging is keyed per Task invocation**: `task-transcript-link.py` chunks the parent transcript into `.claude/state/{subagent_type}/{key}/` and `post-tool-use.py` archives that keyed dir into `…/tasks/{task}/transcripts/{subagent_type}/{key}/` then `rmtree`s it. `subagent_type` is read from the hook's own `tool_input` (via `shared_state.subagent_dir_name`, which sanitises to `[A-Za-z0-9_-]`) and `{key}` is `shared_state.subagent_transcript_key(tool_input)` — identical in the Pre/Post payloads, so the two hooks agree. This isolates parallel same-type subagents (which previously shared one dir and clobbered each other) and the sanitisation keeps the `rmtree` inside `.claude/state/`.

### Configuration Integration
- Agents read from `team-management/config.json`
- Dynamic behavior based on configuration settings
- Graceful degradation for disabled features
- Environment-specific operation adaptation

## Performance and Scalability

### Agent Performance
- Specialized agents for specific operation types
- Efficient file operations and API usage
- Minimal context overhead through isolation
- Parallel agent execution (shipped — brainstorm analysis runs its 6 specialists + AI providers concurrently)

### GitLab API Efficiency
- Intelligent batching of API operations
- Minimal API calls through smart caching
- Rate limit compliance and backoff

### State Management Efficiency
- Efficient mapping storage and retrieval
- Minimal file I/O through smart state management
- Fast status checks without external API calls
- Optimized sync metadata storage

## Testing and Quality Assurance

### Agent Testing
- **Provider-Specific Testing**: Individual agent unit tests for each provider (GitLab, Jira, GitHub/Gitea)
- **Multi-Provider Integration**: Mock external API integration across providers
- **Provider Abstraction Testing**: Tests for provider-agnostic interfaces and routing
- Cross-platform compatibility testing across providers
- Error condition and edge case testing with provider-specific scenarios

### Multi-Provider Integration Testing
- **GitLab Integration**: Mock GitLab API for offline testing, integration tests with real GitLab API
- **Jira Integration**: Mock Jira API testing, Personal Access Token authentication validation
- **Provider Abstraction**: Tests for provider switching and routing logic
- **Cross-Provider**: Rate limiting and error handling validation across providers
- **Conflict Resolution**: Sync conflict resolution testing with multiple provider scenarios
- **Provider-Specific Features**: Testing of unique provider capabilities (GitLab CI/CD, Jira workflows)

### Quality Metrics
- Agent execution time monitoring
- Success/failure rate tracking
- API usage efficiency metrics
- User experience quality assessment

## Security Considerations

### Agent Isolation
- Limited tool access per agent
- No cross-agent communication channels
- Sandboxed execution environment
- State file access restrictions

### GitLab Security
- Secure token handling through configuration
- No token exposure in logs or error messages
- Input validation for all GitLab operations
- Safe handling of external API responses

## Related Documentation

- `plugin/knowledge/claude-code/subagents.md` - Claude Code agent system reference
- `plugin/hooks/CLAUDE.md` - Hook system + provider-utils implementation (single source of truth for GitLab / Jira / GitHub-Gitea integration)
- **Provider-Specific**: Individual provider documentation (GitLab utils, Jira utils, future providers)
- **Architecture**: Provider abstraction and extensibility patterns for adding new issue tracking systems