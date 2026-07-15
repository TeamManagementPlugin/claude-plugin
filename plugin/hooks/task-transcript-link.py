#!/usr/bin/env python3
"""Pre-tool-use hook to chunk transcript for subagents when Task tool is called."""
from collections import deque
try:
    import tiktoken
except ImportError:  # cold plugin session before the venv exists
    tiktoken = None
import json
import sys

# Load input from stdin
try:
    input_data = json.load(sys.stdin)
except json.JSONDecodeError as e:
    print(f"Error: Invalid JSON input: {e}", file=sys.stderr)
    sys.exit(1)

# Check if this is a subagent-dispatch tool call. The tool is named "Task" in
# some Claude Code harnesses and "Agent" in others; recognise both so the
# subagent-depth increment fires in either (the matching PreToolUse matcher in
# plugin/hooks/hooks.json is "Task|Agent").
tool_name = input_data.get("tool_name", "")
if tool_name not in ("Task", "Agent"):
    sys.exit(0)

# Entering a subagent (Task) context — bump the depth counter BEFORE the
# transcript chunking below, so a failure in chunking can't skip the increment.
# The matching decrement happens on Task PostToolUse (post-tool-use.py); a turn
# boundary (UserPromptSubmit / SessionStart) hard-resets it if a Task is denied.
# Skip the bump under workflow bypass: post-tool-use.py early-returns on bypass
# BEFORE its decrement, so incrementing here would desync the counter. Keeping
# both hooks symmetric under bypass avoids drift (harmless either way while
# bypass suppresses enforcement, and UserPromptSubmit resets it each turn).
from shared_state import (
    increment_subagent_depth,
    decrement_subagent_depth,
    check_workflow_bypass,
    _read_file_tail,
    TRANSCRIPT_STAGE_CAP_BYTES,
)
did_increment = False
if not check_workflow_bypass():
    increment_subagent_depth()
    did_increment = True

# From here on, an uncaught crash AFTER the increment above (historically a
# UnicodeEncodeError on Windows cp1252 in the transcript chunk write, before the
# writes gained encoding='utf-8') would ORPHAN the increment: the main agent is
# then treated as a subagent for the rest of the turn and silently bypasses DAIC
# enforcement. So wrap the best-effort chunking: on ANY failure, (a) undo our own
# increment and (b) sys.exit(2) to BLOCK the Task — a blocked Task means
# post-tool-use.py's matching PostToolUse decrement never runs, so the counter
# cannot be double-decremented (a plain decrement + exit 0 would let the Task
# proceed and get decremented a second time, corrupting a parallel sibling's
# count). `except Exception` — NOT BaseException — deliberately lets the normal
# `sys.exit(0)` early-returns below (SystemExit) pass through, keeping the
# increment when the Task proceeds normally (h-fix-discard-clean-and-windows-transcript).
try:
    # Get the transcript path from the input data
    transcript_path = input_data.get("transcript_path", "")
    if not transcript_path:
        sys.exit(0)

    # Get the RECENT TAIL of the transcript into memory (m-fix-unbounded-transcript-reads).
    # The session JSONL grows to tens of MB; reading + tiktoken-encoding all of it
    # on every Task/Agent dispatch is unbounded CPU + RSS in this blocking hook.
    # _read_file_tail bounds the read to the last TRANSCRIPT_STAGE_CAP_BYTES and
    # reports whether the file exceeded that window (`capped`) from the same
    # descriptor observation as the read. It swallows read errors to ([], False),
    # so an unreadable transcript degrades to empty staging and the Task proceeds
    # (exit 0, increment balanced by PostToolUse) rather than blocking — a
    # transient read failure must never wrongly block a dispatch. Per-line guard
    # (C3): a blank / partially-written line is skipped, not crashed on (a crash
    # here lands AFTER the subagent_depth increment and desyncs the counter).
    transcript = []
    tail_lines, capped = _read_file_tail(
        transcript_path, TRANSCRIPT_STAGE_CAP_BYTES, return_capped=True)
    for line in tail_lines:
        if not line.strip():
            continue
        try:
            transcript.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Remove any pre-work transcript entries — but ONLY when the read was NOT
    # capped. On a capped read the Edit/Write/MultiEdit work-start marker may sit
    # BEFORE the tail window, so running the strip loop would pop every in-window
    # entry and starve the subagent. A capped read is already bounded to recent
    # work, so retain it verbatim.
    if not capped:
        start_found = False
        while not start_found and transcript:
            entry = transcript.pop(0)
            message = entry.get('message')
            if message:
                content = message.get('content')
                if isinstance(content, list):
                    for block in content:
                        if block.get('type') == 'tool_use' and block.get('name') in ['Edit', 'MultiEdit', 'Write']:
                            start_found = True

    # Clean the transcript
    clean_transcript = deque()
    for entry in transcript:
        message = entry.get('message')
        message_type = entry.get('type')

        if message and message_type in ['user', 'assistant']:
            content = message.get('content')
            role = message.get('role')
            clean_entry = {
                'role': role,
                'content': content
            }
            clean_transcript.append(clean_entry)

    # Route the transcript by THIS Task call's own subagent_type from the hook
    # payload — NOT by scanning the parent transcript (under parallel Task calls in
    # one message every invocation would resolve to the same last block, and an
    # empty deque would IndexError). The per-invocation key isolates parallel
    # same-type subagents so they don't clobber one staging dir; post-tool-use.py
    # recomputes the same key (tool_input is identical in the Pre/Post payloads) to
    # locate and archive these chunks.
    from shared_state import get_project_root, subagent_transcript_key, subagent_dir_name
    tool_input = input_data.get("tool_input", {})
    subagent_type = subagent_dir_name(tool_input)
    # NOTE: the key is derived from tool_input, so two genuinely-parallel Task calls
    # with byte-identical tool_input (same type AND same prompt) share one staging
    # dir. The hook payload exposes no unique per-invocation id, so this isn't
    # closable here. It is benign: both Pre hooks run before either Post (parallel
    # dispatch), both stage byte-identical chunks (same parent transcript), and on
    # completion whichever PostToolUse runs first archives the full set before
    # rmtree'ing the dir while the other no-ops on the now-removed dir — so the
    # archived chunks are correct (one of two identical copies kept). Distinct-prompt
    # parallel subagents (the common case) get distinct keys.
    invocation_key = subagent_transcript_key(tool_input)

    PROJECT_ROOT = get_project_root()

    # Clear the current transcript directory
    BATCH_DIR = PROJECT_ROOT / '.claude' / 'state' / subagent_type / invocation_key
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    target_dir = BATCH_DIR
    for item in target_dir.iterdir():
        if item.is_file():
            try:
                item.unlink()
            except FileNotFoundError:
                # File already deleted, skip silently
                pass

    # Set up token counting. Without tiktoken (cold session, no venv) fall back to a
    # char-length estimate (~4 chars/token) so chunking still bounds batch size —
    # boundaries become approximate but the hook does not crash.
    if tiktoken is not None:
        enc = tiktoken.get_encoding('cl100k_base')
        def n_tokens(s: str) -> int:
            return len(enc.encode(s))
    else:
        def n_tokens(s: str) -> int:
            return len(s) // 4 + 1

    # Save the transcript in chunks
    MAX_TOKENS_PER_BATCH = 18_000
    transcript_batch, batch_tokens, file_index = [], 0, 1

    while clean_transcript:
        entry = clean_transcript.popleft()
        entry_tokens = n_tokens(json.dumps(entry, ensure_ascii=False))

        if batch_tokens + entry_tokens > MAX_TOKENS_PER_BATCH and transcript_batch:
            file_path = BATCH_DIR / f"current_transcript_{file_index:03}.json"
            with file_path.open('w', encoding='utf-8') as f:
                json.dump(transcript_batch, f, indent=2, ensure_ascii=False)
            file_index += 1
            transcript_batch, batch_tokens = [], 0

        transcript_batch.append(entry)
        batch_tokens += entry_tokens

    if transcript_batch:
        file_path = BATCH_DIR / f'current_transcript_{file_index:03}.json'
        with file_path.open('w', encoding='utf-8') as f:
            json.dump(transcript_batch, f, indent=2, ensure_ascii=False)

    # Allow the tool call to proceed
    sys.exit(0)
except Exception as e:
    # Best-effort chunking failed after the increment. Undo our own increment so
    # the counter is restored, then exit 2 to BLOCK the Task (no PostToolUse ->
    # no double-decrement). SystemExit from the sys.exit(0) paths above is NOT an
    # Exception, so normal early-returns are unaffected.
    if did_increment:
        decrement_subagent_depth()
    print(f"[task-transcript-link] transcript chunking failed after depth "
          f"increment; undid increment and blocking Task: {e}", file=sys.stderr)
    sys.exit(2)
