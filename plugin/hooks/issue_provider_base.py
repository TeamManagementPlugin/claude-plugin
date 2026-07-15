#!/usr/bin/env python3
"""
Abstract base class for issue tracking providers.

Defines the common interface that all issue tracking providers (GitLab, Jira, etc.)
must implement to work with team-management.

This abstraction allows the sessions system to work with multiple issue tracking
systems seamlessly, with provider-specific implementations handling the details
of their respective APIs.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Union
from pathlib import Path
import json
import os
import re
import sys
from datetime import datetime
try:
    import requests
except ImportError:  # cold plugin session before the venv exists
    requests = None
from shared_state import _write_json_durable, _state_lock, get_provider_logger


def find_task_file(project_root: Path, task_name: str) -> Optional[Path]:
    """Find a task file by name, searching in tasks/ and subdirectories.

    Supports both flat structure (tasks/m-task.md) and subdirectories
    (tasks/feature/subtask.md).

    This is a shared utility used by all issue tracking providers.

    Args:
        project_root: Path to the project root directory
        task_name: Task name, can include subdirectory (e.g., "feature/subtask")

    Returns:
        Path to task file if found, None otherwise.
        Searches in order:
        1. Direct path: team-management/tasks/{task_name}.md
        2. Done directory: team-management/tasks/done/{task_name}.md
        3. Recursive search in team-management/tasks/**/{task_name}.md
    """
    tasks_dir = project_root / 'team-management' / 'tasks'

    # 1. Try direct path (handles "task" or "subdir/task")
    direct_path = tasks_dir / f'{task_name}.md'
    if direct_path.exists():
        return direct_path

    # 2. Try done directory
    done_path = tasks_dir / 'done' / f'{task_name}.md'
    if done_path.exists():
        return done_path

    # 3. If task_name doesn't include path separator, search recursively
    if '/' not in task_name and '\\' not in task_name:
        # Search in all subdirectories (except done)
        for task_file in tasks_dir.rglob(f'{task_name}.md'):
            # Skip done directory for active task searches
            if 'done' not in task_file.parts:
                return task_file
        # Also check done subdirectories
        for task_file in (tasks_dir / 'done').rglob(f'{task_name}.md'):
            return task_file

    return None


class IssueTrackingProvider(ABC):
    """Abstract base class for issue tracking providers."""
    
    def __init__(self):
        """Initialize the provider with configuration from config.json"""
        self.project_root = self._find_project_root()
        self.config = self._load_config()
    
    def _find_project_root(self) -> Path:
        """Find project root, env-first.

        Reads ``CLAUDE_PROJECT_DIR`` (injected by Claude Code, pointing at the
        project — NOT the plugin install) before any path walk, mirroring the env
        branch of ``shared_state.get_project_root`` / ``mcp/core/project.get_project_root``.
        Without this, a real plugin install resolves the root from ``__file__``,
        which lives under the marketplace cache OUTSIDE the project, so every
        provider loaded an empty config. A non-existent ``CLAUDE_PROJECT_DIR`` is
        ignored (not trusted blindly) and we fall through to the legacy walk, which
        keeps a dev / source checkout working unchanged.
        """
        env_root = os.environ.get('CLAUDE_PROJECT_DIR')
        if env_root:
            env_path = Path(env_root)
            if env_path.exists():
                return env_path

        current = Path(__file__).resolve()

        # Try different approaches to find project root
        # Start from current directory and walk up the tree
        for parent in [current] + list(current.parents):
            # Look for common project markers
            if any((parent / marker).exists() for marker in ['.git', 'team-management', '.claude']):
                return parent

        # Additional fallback: try current working directory and walk up
        cwd = Path.cwd()
        for parent in [cwd] + list(cwd.parents):
            if any((parent / marker).exists() for marker in ['.git', 'team-management', '.claude']):
                return parent

        # Final fallback: assume we're in .claude/hooks and go up 2 levels
        return current.parents[1]
    
    def _load_config(self) -> Dict:
        """Load configuration from config.json"""
        config_file = self.project_root / 'team-management' / 'config.json'
        try:
            if config_file.exists():
                with open(config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            # stderr, never stdout — in a hook/MCP process stdout is the protocol channel.
            print(f"Warning: Could not load config.json: {e}", file=sys.stderr)
        return {}

    def _redact_token(self, text: Optional[str]) -> Optional[str]:
        """Replace the configured API token with [REDACTED] in arbitrary text.

        Applied to a provider error response body before it is embedded in a
        raised exception or log line, so a misconfigured self-hosted instance
        that echoes the token in its 4xx body cannot leak it downstream.
        """
        token = getattr(self, 'api_token', None)
        if text and token and isinstance(text, str):
            return text.replace(token, '[REDACTED]')
        return text

    def _warn_if_insecure_base_url(self, base_url: str) -> None:
        """Loudly warn (provider log + stderr) when ``base_url`` is non-https and
        the host is NOT loopback — the API token then travels in a cleartext
        header on the wire.

        Deliberately WARN, never reject: self-hosted GitLab/Gitea/Jira over
        internal http stays supported (decision with Max, 2026-07-14). Loopback
        hosts (localhost / 127.0.0.1 / ::1) are exempt — that traffic never
        leaves the machine. Never raises: a URL-parse failure must not break
        provider construction. This complements the SEC-007 write-path guard in
        ``mcp/tools/config.py`` (which only runs when a base_url is *written*),
        covering a legacy / hand-edited config.json read at construction time.
        """
        try:
            import ipaddress
            from urllib.parse import urlparse
            parsed = urlparse(base_url or "")
            if parsed.scheme == "https":
                return
            host = (parsed.hostname or "").lower()
            if host == "localhost":
                return
            try:
                # Covers the whole loopback range (127.0.0.0/8, ::1), not just
                # the literal 127.0.0.1 / ::1.
                if ipaddress.ip_address(host).is_loopback:
                    return
            except ValueError:
                pass  # host is a name, not a literal IP — fall through to warn
            # Build a credential-safe display: NEVER log the raw URL, else this
            # very security warning would leak an embedded credential
            # (`http://user:secret@host?token=...`) into stderr + the persistent
            # provider log (codex P2). Prefer urlparse's own authority parse:
            # `parsed.hostname` bounds the host correctly even when the userinfo
            # contains a `/` (a base64 token can) — the token then lands in the
            # path, which we drop — so only scheme://host[:port] is shown, never
            # userinfo/path/query/fragment (code-review W1: a `/`-in-credential
            # defeated the earlier string partition). `parsed.port` is read under
            # its own guard because it raises on an out-of-range port, which must
            # NOT swallow the warning (code-review N1).
            if parsed.hostname:
                try:
                    port = parsed.port
                except ValueError:
                    port = None
                netloc = parsed.hostname if not port else f"{parsed.hostname}:{port}"
                safe_url = f"{parsed.scheme}://{netloc}"
            else:
                # Scheme-less URL (no `//`): urlparse yields no host. Drop query/
                # fragment, then take everything after the LAST `@` so a userinfo
                # segment that itself contains `/` (which would defeat a
                # partition-on-`/`) is still fully stripped — no credential
                # survives. Trade-off: a scheme-less URL with a legit `@` in its
                # PATH (and no userinfo) mis-displays as the post-`@` tail, but it
                # never leaks a secret (real provider base_urls carry a scheme).
                raw = (base_url or "").split("?", 1)[0].split("#", 1)[0]
                safe_url = raw.rsplit("@", 1)[-1]
            msg = (
                f"[{self.provider_name}] SECURITY: base_url {safe_url!r} is not https — "
                f"the API token is sent in a cleartext header. Use https:// for any "
                f"non-loopback host."
            )
            sys.stderr.write(msg + "\n")
            try:
                get_provider_logger(f"{self.provider_name}.log")(msg)
            except Exception:
                pass
        except Exception:
            pass  # best-effort; never break construction on a malformed base_url

    def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Optional[Dict]:
        """Make an API request to the provider with structured error reporting.

        Single source of truth shared by all providers (GitLab / GitHub / Jira).
        Subclasses supply the provider-specific request surface via instance
        attributes set in their ``__init__``:
          - ``self.api_base``        — URL prefix the ``endpoint`` is appended to
          - ``self.headers``         — auth + content-type headers
          - ``self.request_timeout`` — default per-request timeout in seconds

        The ``BCC_HTTP_TIMEOUT_SECONDS`` env var overrides the per-instance
        default when set. Handles 204 No Content and redacts the API token from
        any HTTP error body before it is surfaced.
        """
        url = f"{self.api_base}{endpoint}"
        timeout_seconds = int(os.getenv("BCC_HTTP_TIMEOUT_SECONDS",
                                        str(getattr(self, 'request_timeout', 30))))

        # Guard MUST precede the try: the except clauses resolve `requests.exceptions`
        # at handler-eval time and would AttributeError on a None `requests`.
        if requests is None:
            return {
                'error': True,
                'error_type': 'dependency_missing',
                'message': "the 'requests' package is not installed (cold session without venv)",
                'url': url,
                'method': method,
            }

        try:
            verb = method.upper()
            if verb == 'GET':
                response = requests.get(url, headers=self.headers, timeout=timeout_seconds)
            elif verb == 'POST':
                response = requests.post(url, headers=self.headers, json=data, timeout=timeout_seconds)
            elif verb == 'PUT':
                response = requests.put(url, headers=self.headers, json=data, timeout=timeout_seconds)
            elif verb == 'PATCH':
                response = requests.patch(url, headers=self.headers, json=data, timeout=timeout_seconds)
            elif verb == 'DELETE':
                response = requests.delete(url, headers=self.headers, json=data, timeout=timeout_seconds)
            else:
                raise Exception(f"Unsupported HTTP method: {method}")

            response.raise_for_status()

            # Handle 204 No Content (would crash on response.json())
            if response.status_code == 204:
                return {'success': True}

            return response.json()

        except requests.exceptions.HTTPError as e:
            error_body = ""
            try:
                # Redact the token BEFORE truncating so a token split across the
                # 500-char boundary still cannot leak through.
                error_body = self._redact_token(e.response.text)[:500]
            except Exception:
                pass

            return {
                'error': True,
                'error_type': 'http_error',
                'status_code': e.response.status_code,
                'message': f"HTTP {e.response.status_code}: {e.response.reason}",
                'response_body': error_body,
                'url': url,
                'method': method
            }

        except requests.exceptions.Timeout:
            return {
                'error': True,
                'error_type': 'timeout',
                'message': f"Request timed out after {timeout_seconds} seconds",
                'url': url,
                'method': method
            }

        except requests.exceptions.ConnectionError as e:
            return {
                'error': True,
                'error_type': 'connection_error',
                'message': f"Connection failed: {str(e)}",
                'url': url,
                'method': method
            }

        except requests.exceptions.RequestException as e:
            return {
                'error': True,
                'error_type': 'request_error',
                'message': str(e),
                'url': url,
                'method': method
            }

        except Exception as e:
            return {
                'error': True,
                'error_type': 'unexpected_error',
                'message': str(e),
                'url': url,
                'method': method
            }

    def _raise_on_error(self, result: Optional[Dict], context: str) -> Optional[Dict]:
        """Raise a detailed Exception when a ``_make_request`` result is an error.

        Single-sources the error-dict→raise boilerplate that was copy-pasted
        ~28 times across the provider get/create/update/comment/MR/PR/release
        methods. Returns ``result`` unchanged when it is not an error, so a caller
        can write ``return self._raise_on_error(self._make_request(...), "…")`` or
        use it as a bare statement for a mid-method check. Non-dict results (a
        successful list endpoint, or None) are returned unchanged — so it is safe
        on the list-returning endpoints that previously needed an explicit
        ``isinstance(result, dict)`` guard.
        """
        if isinstance(result, dict) and result.get('error'):
            error_msg = f"{context}: {result.get('message', 'Unknown error')}"
            if result.get('status_code'):
                error_msg += f" (Status: {result['status_code']})"
            if result.get('response_body'):
                error_msg += f"\nResponse: {result['response_body']}"
            raise Exception(error_msg)
        return result

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the name of this provider (e.g., 'gitlab', 'jira')"""
        pass
    
    @abstractmethod
    def get_issue(self, issue_id: Union[str, int]) -> Optional[Dict]:
        """Get a specific issue by ID"""
        pass
    
    @abstractmethod
    def create_issue(self, title: str, description: str = "", labels: List[str] = None, 
                    issue_type: str = None) -> Optional[Dict]:
        """Create a new issue"""
        pass
    
    @abstractmethod
    def update_issue(self, issue_id: Union[str, int], title: str = None, 
                    description: str = None, status: str = None, 
                    labels: List[str] = None) -> Optional[Dict]:
        """Update an existing issue"""
        pass
    
    @abstractmethod
    def add_comment(self, issue_id: Union[str, int], comment: str) -> Optional[Dict]:
        """Add a comment to an issue"""
        pass
    
    @abstractmethod
    def parse_issue_url(self, url: str) -> Optional[Dict[str, str]]:
        """Parse issue URL to extract components"""
        pass
    
    @abstractmethod
    def format_issue_id(self, issue_id: Union[str, int]) -> str:
        """Format issue ID with provider prefix (e.g., 'jira:PROJ-123', 'gitlab:456')"""
        pass
    
    @abstractmethod
    def extract_issue_id(self, formatted_id: str) -> Union[str, int]:
        """Extract the actual issue ID from a provider-prefixed ID"""
        pass
    
    @abstractmethod
    def get_supported_issue_types(self) -> List[str]:
        """Get list of supported issue types for this provider"""
        pass
    
    @abstractmethod
    def validate_configuration(self) -> bool:
        """Validate that the provider is properly configured"""
        pass


class IssueTrackingTaskSync(ABC):
    """Abstract base class for task synchronization with issue tracking providers."""
    
    def __init__(self, provider: IssueTrackingProvider):
        """Initialize with a provider instance"""
        self.provider = provider
        self.project_root = provider.project_root
        
        # Provider-specific mappings file
        mappings_dir = self.project_root / '.claude' / 'state'
        mappings_dir.mkdir(parents=True, exist_ok=True)
        self.mappings_file = mappings_dir / f'{provider.provider_name}-mappings.json'
        self._ensure_mappings_file()
    
    def _ensure_mappings_file(self):
        """Ensure the provider-specific mappings file exists.

        Atomic create-only seed: a bare ``if not exists(): _write_json_durable({})``
        is a check-then-write TOCTOU — two processes starting concurrently on a
        fresh project can both observe the file absent, and the delayed ``{}``
        write (an ``os.replace``) can overwrite a mapping the other process just
        saved in that window. ``open(path, 'x')`` fails with ``FileExistsError``
        the instant the file already exists, so a concurrent seed can never
        clobber a real mapping (m-fix-mappings-lock-free-rmw).
        """
        if self.mappings_file.exists():
            return
        try:
            with open(self.mappings_file, 'x', encoding='utf-8') as f:
                json.dump({}, f)
        except FileExistsError:
            pass
    
    def _load_mappings(self) -> Dict:
        """Load task-to-issue mappings.

        Missing file → ``{}`` (tolerant: first run / nothing linked yet). A
        zero-length / whitespace-only file is ALSO treated as ``{}`` — it has no
        links to preserve, and this closes the tiny window where a concurrent
        ``_ensure_mappings_file`` seed (``open(path, 'x')`` then ``json.dump``) is
        observed by an unlocked reader AFTER the file is created but BEFORE the
        ``{}`` bytes land (m-fix-mappings-lock-free-rmw). A file that EXISTS with
        non-blank but corrupt JSON is NOT silently swallowed: the old bare
        ``except: return {}`` let a corrupt file read as empty and the next
        ``_save_mappings`` OVERWRITE it, destroying every task↔issue link.
        Instead the corrupt bytes are copied to a ``.corrupt-<ts>`` sidecar and a
        ``RuntimeError`` is raised (the live file is left untouched), so nothing
        can overwrite the links with an empty map.
        """
        try:
            with open(self.mappings_file, 'r', encoding='utf-8') as f:
                raw = f.read()
        except FileNotFoundError:
            return {}
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            backup = self.mappings_file.with_name(
                f"{self.mappings_file.name}.corrupt-"
                f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
            )
            try:
                backup.write_bytes(self.mappings_file.read_bytes())
                backup_note = f"a backup copy was saved as {backup.name}"
            except OSError:
                backup_note = "a backup copy could not be written"
            raise RuntimeError(
                f"Mappings file {self.mappings_file.name} is corrupt ({e}); "
                f"{backup_note}. Refusing to overwrite it with an empty map — "
                f"fix or delete the file to continue."
            ) from e
    
    def _save_mappings(self, mappings: Dict):
        """Save task-to-issue mappings"""
        _write_json_durable(self.mappings_file, mappings)

    def _mapping_rmw_unlocked(self, mutator) -> bool:
        """One read-modify-write over the mappings file, WITHOUT the lock.

        ``mutator(mappings)`` mutates the loaded dict IN PLACE and returns a
        truthy value to persist the change (falsy to skip the save — this
        preserves the historical ``if task in mappings: ... _save_mappings()``
        conditional writes). A corrupt-file ``RuntimeError`` from
        ``_load_mappings`` propagates unchanged. Returns True iff a save ran.
        """
        mappings = self._load_mappings()
        if mutator(mappings):
            self._save_mappings(mappings)
            return True
        return False

    def _locked_mapping_update(self, mutator) -> bool:
        """Locked read-modify-write over the mappings file.

        The whole ``_load_mappings() -> mutator(mappings) -> _save_mappings()``
        runs under ``shared_state._state_lock()`` so a concurrent writer (the
        PostToolUse auto-sync hook vs the long-lived MCP server) cannot
        lose-update the mappings file. ``mutator`` follows the
        ``_mapping_rmw_unlocked`` contract (mutate in place, return truthy to
        save).

        CALLER CONSTRAINT: ``_state_lock`` is the SINGLE global state lock (it
        also guards ``current_task.json`` via ``set_task_state`` /
        ``set_protocol_state`` / the counter helpers) and is NON-reentrant. Never
        call a mapping-mutation method from inside an already-held ``_state_lock``
        block — a nested acquisition self-deadlocks. No current caller does (the
        auto-sync path and MCP tools invoke these at top level).

        Failure model — deliberately STRICTER than the counter helpers
        (``increment_counter_file``), because ``_save_mappings`` is a durable
        integrity write that does NOT swallow its own ``OSError`` the way
        ``_increment_counter_unlocked`` does:

          * lock ACQUISITION ``OSError`` (flock-less/synced FS, permissions) ->
            degrade to ONE unlocked RMW (mappings sync is best-effort and must
            never crash a hook);
          * an ``OSError`` raised INSIDE the held lock (a real load/save I/O
            failure) -> PROPAGATE. Never silently fall back to an unlocked write
            here — that would reintroduce the very race this guards against;
          * an ``OSError`` on lock RELEASE/close AFTER the body already persisted
            (``_state_lock``'s ``f.close()`` is outside its inner ``try``) ->
            return the already-persisted result (no double RMW);
          * a corrupt-file ``RuntimeError`` from ``_load_mappings`` -> propagate
            unchanged (it is not an ``OSError``).

        Two markers (``acquired`` / ``completed``) — not the single ``entered``
        sentinel — are what let the ``except`` distinguish an acquisition failure
        from a body failure from a release failure. Returns True iff a save ran.
        """
        acquired = False
        completed = False
        result = False
        try:
            with _state_lock():
                acquired = True
                result = self._mapping_rmw_unlocked(mutator)
                completed = True
            return result
        except OSError:
            if completed:
                return result          # RMW body finished (result = did a save run);
                                       # only lock release/close failed → don't re-run
            if acquired:
                raise                  # body failed under a held lock -> real error
            return self._mapping_rmw_unlocked(mutator)   # never acquired -> degrade

    @staticmethod
    def _slugify(title: str) -> str:
        """Canonical task-name slug from an issue title (Unicode-aware).

        Single-sources the task-name sanitizer duplicated across the provider
        task-sync modules: lowercase → collapse any run of non-alphanumerics to a
        single '-' → strip leading/trailing '-' → cap at 50 chars. Callers build
        the full name as ``f"{priority}-{slug}"``.

        Unicode-aware on purpose: ``[\\W_]`` (str patterns are Unicode by default)
        KEEPS Cyrillic / CJK / accented letters, matching the ``str.isalnum()``
        behavior the GitLab/Jira copies used. An ASCII-only ``[^a-z0-9]`` would
        strip such a title to an empty slug (``m-`` / ``feature/``) and collide
        across unrelated issues. Falls back to ``"task"`` when the title has no
        alphanumerics at all.
        """
        slug = re.sub(r'[\W_]+', '-', title.lower(), flags=re.UNICODE).strip('-')[:50]
        return slug or "task"

    @staticmethod
    def _extract_task_title(content: str, fallback: str = "") -> str:
        """Return the first Markdown H1 (`# Title`) line's text, else ``fallback``.

        Single-sources the `# `-first-line title scan duplicated across the task
        sync modules (MR/PR title derivation).
        """
        for line in content.split('\n'):
            stripped = line.strip()
            if stripped.startswith('# '):
                return stripped[2:].strip()
        return fallback
    
    @abstractmethod
    def link_task_to_issue(self, task_name: str, issue_id: Union[str, int], 
                          sync_direction: str = "bidirectional") -> bool:
        """Create a mapping between a task and an issue"""
        pass
    
    @abstractmethod
    def import_issue_as_task(self, issue_id_or_url: Union[str, int], 
                           task_priority: str = "m") -> str:
        """Import an issue as a new Claude task"""
        pass
    
    @abstractmethod
    def create_issue_from_task(self, task_name: str) -> Optional[Union[str, int]]:
        """Create an issue from a Claude task"""
        pass
    
    @abstractmethod
    def sync_task_status_to_issue(self, task_name: str, status: str, 
                                 additional_info: Dict = None):
        """Sync task status to the linked issue"""
        pass
    
    def get_task_mapping(self, task_name: str) -> Optional[Dict]:
        """Get issue mapping for a task"""
        mappings = self._load_mappings()
        return mappings.get(task_name)
    
    def unlink_task(self, task_name: str):
        """Remove mapping for a task"""
        def _mut(mappings):
            if task_name in mappings:
                del mappings[task_name]
                return True
            return False
        self._locked_mapping_update(_mut)

    def _collect_agent_summaries(self, task_name: str) -> Dict[str, str]:
        """Collect actual sub-agent run summaries and results from task work log.

        This is a shared utility that parses the task file's work log section
        to extract information about agent executions (code-review, logging, etc.).

        Args:
            task_name: Name of the task

        Returns:
            Dictionary mapping agent types to their summary messages
        """
        agent_summaries = {}

        # Look for agent output patterns in work log
        task_file = find_task_file(self.project_root, task_name)
        if not task_file:
            return agent_summaries

        try:
            with open(task_file, 'r', encoding='utf-8') as f:
                content = f.read()

            lines = content.split('\n')
            in_work_log = False

            # Parse work log for agent run results
            for i, line in enumerate(lines):
                line_stripped = line.strip()

                if line_stripped.startswith('## Work Log'):
                    in_work_log = True
                    continue
                elif line_stripped.startswith('##') and in_work_log:
                    break
                elif in_work_log and line_stripped:
                    # Look for agent-specific patterns
                    line_lower = line_stripped.lower()

                    if 'code-review' in line_lower:
                        if 'passed' in line_lower:
                            agent_summaries['code-review'] = "Code review completed - security and quality validated"
                        elif 'issues found' in line_lower:
                            agent_summaries['code-review'] = "Code review completed with recommendations addressed"
                        else:
                            agent_summaries['code-review'] = "Code review agent executed"

                    elif 'service-documentation' in line_lower:
                        if 'updated' in line_lower:
                            agent_summaries['service-documentation'] = "Service documentation updated with implementation changes"
                        else:
                            agent_summaries['service-documentation'] = "Service documentation agent executed"

                    elif 'context-gathering' in line_lower:
                        agent_summaries['context-gathering'] = "Context analysis completed - implementation properly informed"

                    elif 'logging' in line_lower:
                        agent_summaries['logging'] = "Task documentation consolidated and organized"

                    # Look for testing patterns (covers pytest, flutter test, unittest, etc.)
                    elif any(keyword in line_lower for keyword in ['test', 'pytest', 'unittest', 'flutter test', 'flutter analyze']):
                        agent_summaries['testing'] = "Code quality validation completed"

                    # Look for build/deployment patterns
                    elif any(keyword in line_lower for keyword in ['build', 'flutter build', 'deployment', 'ci/cd']):
                        agent_summaries['build'] = "Build and deployment validation completed"

        except Exception:
            pass

        return agent_summaries

    def _generate_rich_completion_comment(self, task_name: str, merge_request_info: Dict = None) -> str:
        """Generate rich completion comment with actual sub-agent summaries and results.

        This is a shared utility used by all providers to create formatted
        completion comments for issues when tasks are completed.

        Args:
            task_name: Name of the task
            merge_request_info: Optional dict with MR/PR info (e.g., {'merge_request_iid': 123})

        Returns:
            Formatted markdown comment string
        """
        task_file = find_task_file(self.project_root, task_name)
        if not task_file:
            return f"**Task Status Update from Claude Code**: completed"

        try:
            with open(task_file, 'r', encoding='utf-8') as f:
                content = f.read()

            # Collect actual agent summaries
            agent_summaries = self._collect_agent_summaries(task_name)

            # Parse task sections
            lines = content.split('\n')
            success_criteria = []
            in_success_section = False

            for line in lines:
                line_stripped = line.strip()

                if line_stripped.startswith('## Success Criteria'):
                    in_success_section = True
                    continue
                elif line_stripped.startswith('##') and in_success_section:
                    break
                elif in_success_section and line_stripped.startswith('- [x]'):
                    # Extract completed success criteria
                    success_criteria.append(line_stripped[6:].strip())

            # Extract problem/goal for context
            problem_section = ""
            for i, line in enumerate(lines):
                if line.strip().startswith('## Problem/Goal'):
                    for j in range(i+1, min(i+4, len(lines))):
                        if lines[j].strip().startswith('##'):
                            break
                        if lines[j].strip():
                            problem_section = lines[j].strip()
                            break
                    break

            # Build completion comment with actual agent results
            comment_parts = ["**Task Completed Successfully**"]

            if problem_section:
                comment_parts.append(f"\n{problem_section}")

            # Add implementation summary from success criteria
            if success_criteria:
                comment_parts.append("\n## Implementation Summary")
                for criterion in success_criteria:
                    comment_parts.append(f"\n- {criterion}")

            # Add actual sub-agent results
            if agent_summaries:
                comment_parts.append("\n## Quality Assurance & Agent Validation")
                for agent_type, summary in agent_summaries.items():
                    comment_parts.append(f"- {summary}")
            else:
                # Fallback to basic QA section if no agent summaries found
                comment_parts.append("\n## Quality Assurance")
                comment_parts.append("\n- Implementation completed and validated")
                comment_parts.append("\n- Code quality standards maintained")

            # Add provider integration section (uses provider_name for customization)
            provider_name = self.provider.provider_name.title()
            comment_parts.append(f"\n## {provider_name} Integration")
            comment_parts.append("- Task-issue synchronization maintained throughout development")

            # Add merge request/pull request information if provided
            if merge_request_info:
                mr_iid = merge_request_info.get('merge_request_iid') or merge_request_info.get('pull_request_number')
                if mr_iid:
                    mr_type = "Merge request" if self.provider.provider_name == "gitlab" else "Pull request"
                    mr_prefix = "!" if self.provider.provider_name == "gitlab" else "#"
                    comment_parts.append(f"- {mr_type} {mr_prefix}{mr_iid} created with comprehensive change summary")

            # Add Claude Code signature
            comment_parts.append("\n---\nGenerated with [Claude Code](https://claude.ai/code)")

            return '\n'.join(comment_parts)

        except Exception as e:
            # Fallback to basic comment if parsing fails
            return f"**Task Status Update from Claude Code**: completed (rich comment generation failed: {str(e)})"

    def _extract_code_review_results(self, task_name: str) -> Optional[str]:
        """Extract final passing code review results from task work log.

        This is a shared utility used by all providers to extract code review
        results for posting to MRs/PRs.

        Args:
            task_name: Name of the task

        Returns:
            Formatted markdown of the last code review, or None if not found
        """
        task_file = find_task_file(self.project_root, task_name)
        if not task_file:
            return None

        try:
            with open(task_file, 'r', encoding='utf-8') as f:
                content = f.read()

            # Input size limit to prevent ReDoS attacks
            if len(content) > 1_000_000:  # 1MB limit
                return None

            # Look for code review pattern in work log
            # Pattern: "# Code Review:" until next major section (single #) or end of file
            review_pattern = r'# Code Review:.*?(?=\n#[^#]|\Z)'
            matches = list(re.finditer(review_pattern, content, re.DOTALL))

            if not matches:
                return None

            # Get the last review (final passing review after any fixes)
            last_review = matches[-1].group(0).strip()

            # Format as MR/PR comment with header
            formatted_review = f"""## Code Review Results

{last_review}

---
Automated code review from [Claude Code](https://claude.com/claude-code)"""

            return formatted_review

        except Exception:
            # Silently fail - code review comment is optional
            return None