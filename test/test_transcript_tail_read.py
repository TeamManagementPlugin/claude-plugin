#!/usr/bin/env python3
"""Bounded transcript tail-read (m-statusline-and-test-infra, SC#1).

The statusline runs on EVERY prompt. `find_current_transcript`
(`plugin/templates/statusline.py`) and `get_context_length_from_transcript`
(`plugin/hooks/shared_state.py`) previously `readlines()`'d the ENTIRE session
JSONL (tens of MB in long sessions) just to reach the tail, so statusline
latency grew unboundedly with session length.

The fix reads only the last `TRANSCRIPT_TAIL_BYTES` via a seek-from-end
primitive (`_read_file_tail` / `read_last_jsonl_entry`), and
`get_context_length_from_transcript` reverse-scans that tail for the newest
main-chain (non-sidechain) `message.usage` entry. That reader ALSO feeds the
auto-compact monitors in `user-messages.py` / `post-tool-use.py`, so it must
stay CORRECT (not just fast): a naive fixed window that returned 0 whenever the
newest usage sat behind a run of sidechain entries would silently disable
auto-compact. It degrades to 0 only when no eligible entry falls in the window
(documented bound), which in practice never happens because the newest
main-chain usage sits within a few KB of EOF.

Run: python3 -m pytest test/test_transcript_tail_read.py -v
"""
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS))

import shared_state  # noqa: E402


def _write_jsonl(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _usage_entry(input_tokens, cache_read=0, cache_creation=0, sidechain=False, ts="2026-07-03T00:00:00Z"):
    e = {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "usage": {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        }},
    }
    if sidechain:
        e["isSidechain"] = True
    return e


# ----------------------------------------------------------- read_last_jsonl_entry

def test_read_last_jsonl_entry_returns_last(tmp_path):
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [{"i": 1}, {"i": 2}, {"i": 3, "last": True}])
    assert shared_state.read_last_jsonl_entry(str(f)) == {"i": 3, "last": True}


def test_read_last_jsonl_entry_skips_trailing_blank(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text(json.dumps({"i": 1}) + "\n" + json.dumps({"i": 2}) + "\n\n\n", encoding="utf-8")
    assert shared_state.read_last_jsonl_entry(str(f)) == {"i": 2}


def test_read_last_jsonl_entry_missing_file(tmp_path):
    assert shared_state.read_last_jsonl_entry(str(tmp_path / "nope.jsonl")) is None


def test_read_last_jsonl_entry_skips_unparseable_tail(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text(json.dumps({"i": 1, "good": True}) + "\n" + "{not json\n", encoding="utf-8")
    assert shared_state.read_last_jsonl_entry(str(f)) == {"i": 1, "good": True}


def test_read_last_jsonl_entry_tail_window_seeks_end(tmp_path):
    # With a small window, only the tail is consulted; the partial first line of
    # the window must be dropped (never returned truncated).
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [{"i": n} for n in range(500)])
    got = shared_state.read_last_jsonl_entry(str(f), max_bytes=64)
    assert got == {"i": 499}


def test_read_last_jsonl_entry_oversized_final_line_falls_back(tmp_path):
    # (codex re-review) When the FINAL record is larger than the window, the tail
    # holds only a fragment of it -> a tail-only read returns None and
    # find_current_transcript would lose the last timestamp/sessionId. The
    # full-file fallback must still return the complete final entry.
    f = tmp_path / "t.jsonl"
    last = {"i": 2, "sessionId": "s-xyz", "content": "z" * 4000}
    _write_jsonl(f, [{"i": 1}, last])
    got = shared_state.read_last_jsonl_entry(str(f), max_bytes=512)
    assert got == last


# ------------------------------------------------ get_context_length_from_transcript

def test_context_length_basic(tmp_path):
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        _usage_entry(100, cache_read=1000, cache_creation=50),
    ])
    assert shared_state.get_context_length_from_transcript(str(f)) == 1150


def test_context_length_picks_newest_main_chain(tmp_path):
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        _usage_entry(100),
        _usage_entry(9999),  # newest main-chain -> this one wins
    ])
    assert shared_state.get_context_length_from_transcript(str(f)) == 9999


def test_context_length_ignores_sidechain_tail(tmp_path):
    # The newest main-chain usage is BEFORE a run of sidechain (subagent) entries
    # at the very tail. A correct reader returns the main-chain value, not 0 and
    # not the sidechain usage.
    f = tmp_path / "t.jsonl"
    entries = [_usage_entry(4242)]
    entries += [_usage_entry(7, sidechain=True) for _ in range(20)]
    _write_jsonl(f, entries)
    assert shared_state.get_context_length_from_transcript(str(f)) == 4242


def test_context_length_missing_file(tmp_path):
    assert shared_state.get_context_length_from_transcript(str(tmp_path / "nope.jsonl")) == 0


def test_context_length_no_usage_returns_zero(tmp_path):
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [{"type": "user", "message": {"role": "user", "content": "hi"}}])
    assert shared_state.get_context_length_from_transcript(str(f)) == 0


def test_context_length_bad_message_shape_does_not_crash(tmp_path):
    # A line whose `message` is a string (not a dict) must not abort the whole
    # scan; the earlier eligible entry is still returned.
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        _usage_entry(321),
        {"type": "assistant", "message": "oops-a-string"},
    ])
    assert shared_state.get_context_length_from_transcript(str(f)) == 321


def test_context_length_skips_odd_entry_shapes(tmp_path):
    # (codex review) The reverse scan must skip every non-eligible shape and land
    # on the one non-sidechain dict-message-with-usage entry: message=null,
    # message="text", a summary entry, a sidechain entry that ALSO has usage, and
    # a truncated (unparseable) final line.
    f = tmp_path / "t.jsonl"
    f.write_text(
        json.dumps(_usage_entry(777)) + "\n"
        + json.dumps({"type": "assistant", "message": None}) + "\n"
        + json.dumps({"type": "summary", "summary": "compacted"}) + "\n"
        + json.dumps({"type": "assistant", "message": "text"}) + "\n"
        + json.dumps(_usage_entry(9, sidechain=True)) + "\n"
        + '{"truncated": ',  # no newline, unparseable final line
        encoding="utf-8",
    )
    assert shared_state.get_context_length_from_transcript(str(f)) == 777


def test_context_length_window_boundary_on_newline(tmp_path):
    # (codex review) When the tail window starts EXACTLY on a line boundary, the
    # first in-window line is complete and must NOT be dropped. Size the window so
    # read_from lands right after a newline, on the sole usage entry.
    f = tmp_path / "t.jsonl"
    tail_entry = json.dumps(_usage_entry(4096)) + "\n"
    prefix = json.dumps({"type": "user", "message": {"role": "user", "content": "p"}}) + "\n"
    f.write_text(prefix + tail_entry, encoding="utf-8")
    # window == exactly the tail entry's byte length -> read_from == len(prefix),
    # i.e. the byte before the window is the prefix's trailing newline.
    got = shared_state.get_context_length_from_transcript(str(f), max_bytes=len(tail_entry.encode("utf-8")))
    assert got == 4096, got


def test_context_length_fallback_finds_usage_past_window(tmp_path):
    # (codex P2 + code-review W1) When the tail window holds no eligible entry
    # (newest usage pushed past the window by later non-usage lines), the reader
    # FALLS BACK to a full scan and still finds it — no auto-compact blind spot.
    f = tmp_path / "t.jsonl"
    entries = [_usage_entry(5000)]  # early, big
    entries += [{"type": "user", "message": {"role": "user", "content": "x" * 200}} for _ in range(50)]
    _write_jsonl(f, entries)
    assert shared_state.get_context_length_from_transcript(str(f), max_bytes=256) == 5000


def test_context_length_oversized_final_line_falls_back(tmp_path):
    # The exact reviewer scenario: the FINAL line is a single entry LARGER than
    # the window (a big tool result / paste). The newest assistant usage is the
    # line before it. A tail-only read returns 0; the fallback returns the usage.
    f = tmp_path / "t.jsonl"
    huge = {"type": "user", "message": {"role": "user", "content": "z" * 4000}}
    _write_jsonl(f, [_usage_entry(8080), huge])
    assert shared_state.get_context_length_from_transcript(str(f), max_bytes=512) == 8080


def test_context_length_no_usage_anywhere_returns_zero_even_with_fallback(tmp_path):
    # Fallback that finds nothing still returns 0 (genuinely no usage in the file).
    f = tmp_path / "t.jsonl"
    entries = [{"type": "user", "message": {"role": "user", "content": "x" * 200}} for _ in range(50)]
    _write_jsonl(f, entries)
    assert shared_state.get_context_length_from_transcript(str(f), max_bytes=256) == 0


# --------------------------------------------------------------- flat runtime (50MB)

def test_context_length_flat_runtime_on_50mb(tmp_path):
    """SC#1: statusline runtime flat w.r.t. transcript size. Build a ~50MB JSONL
    whose newest main-chain usage is the LAST line; assert it is returned AND the
    call completes quickly (bounded tail-read never touches the 50MB prefix)."""
    f = tmp_path / "big.jsonl"
    filler = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 900}}) + "\n"
    block = filler * 1000  # ~0.95 MB
    with open(f, "w", encoding="utf-8") as fh:
        for _ in range(56):  # ~53 MB, comfortably over 50 MiB
            fh.write(block)
        fh.write(json.dumps(_usage_entry(123, cache_read=1000, cache_creation=77)) + "\n")
    assert f.stat().st_size > 50 * 1024 * 1024

    start = time.monotonic()
    got = shared_state.get_context_length_from_transcript(str(f))
    elapsed = time.monotonic() - start

    assert got == 1200, got
    assert elapsed < 3.0, f"tail-read should be flat/fast, took {elapsed:.2f}s"


# ----------------------------------------------------------- get_model_from_transcript

def _model_entry(model, ts="2026-07-03T00:00:00Z"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "model": model}}


def test_get_model_basic(tmp_path):
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        _model_entry("claude-opus-4-8"),
    ])
    assert shared_state.get_model_from_transcript(str(f)) == "claude-opus-4-8"


def test_get_model_picks_newest(tmp_path):
    # Two model entries -> the newest (last in append order) wins.
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [_model_entry("claude-old"), _model_entry("claude-new")])
    assert shared_state.get_model_from_transcript(str(f)) == "claude-new"


def test_get_model_found_beyond_last_10_entries(tmp_path):
    # (codex) The model sits 11 entries from EOF, behind 10 model-less user
    # turns. The old code inspected only lines[-10:] and returned "unknown"; the
    # reverse tail scan finds it. This is the intentional, strictly-more-correct
    # divergence from the old final-10 behavior.
    f = tmp_path / "t.jsonl"
    entries = [_model_entry("claude-deep")]
    entries += [{"type": "user", "message": {"role": "user", "content": "x"}} for _ in range(10)]
    _write_jsonl(f, entries)
    assert shared_state.get_model_from_transcript(str(f)) == "claude-deep"


def test_get_model_missing_file(tmp_path):
    assert shared_state.get_model_from_transcript(str(tmp_path / "nope.jsonl")) == "unknown"


def test_get_model_no_model_returns_unknown(tmp_path):
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [{"type": "user", "message": {"role": "user", "content": "hi"}}])
    assert shared_state.get_model_from_transcript(str(f)) == "unknown"


def test_get_model_bad_message_shape_does_not_crash(tmp_path):
    # A string `message` (not a dict) must not abort the scan; the earlier
    # model-bearing entry is still returned (the old code lacked this guard and
    # bailed to the outer except -> "unknown").
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        _model_entry("claude-ok"),
        {"type": "assistant", "message": "oops-a-string"},
    ])
    assert shared_state.get_model_from_transcript(str(f)) == "claude-ok"


def test_get_model_skips_unparseable_tail(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text(json.dumps(_model_entry("claude-ok")) + "\n" + "{not json\n", encoding="utf-8")
    assert shared_state.get_model_from_transcript(str(f)) == "claude-ok"


def test_get_model_skips_non_dict_line(tmp_path):
    # (codex) A valid-JSON but non-dict top-level line (a list / a bare string)
    # must be skipped, not crash on data.get(...). The earlier model entry wins.
    f = tmp_path / "t.jsonl"
    f.write_text(
        json.dumps(_model_entry("claude-ok")) + "\n"
        + json.dumps([1, 2, 3]) + "\n"       # top-level list
        + json.dumps("just a string") + "\n",  # top-level string
        encoding="utf-8",
    )
    assert shared_state.get_model_from_transcript(str(f)) == "claude-ok"


def test_get_model_tail_window_seeks_end(tmp_path):
    # Small window: only the tail is consulted; the partial first line of the
    # window is dropped. The model on the last entry is found within the window.
    f = tmp_path / "t.jsonl"
    entries = [{"type": "user", "message": {"role": "user", "content": "x" * 40}} for _ in range(500)]
    entries.append(_model_entry("claude-tail"))
    _write_jsonl(f, entries)
    assert shared_state.get_model_from_transcript(str(f), max_bytes=128) == "claude-tail"


def test_get_model_past_window_falls_back(tmp_path):
    # The model sits early, then many model-less user lines push it past the small
    # window. The tail holds no model -> full-file fallback finds it (mirrors
    # get_context_length_from_transcript's fallback).
    f = tmp_path / "t.jsonl"
    entries = [_model_entry("claude-early")]
    entries += [{"type": "user", "message": {"role": "user", "content": "x" * 200}} for _ in range(50)]
    _write_jsonl(f, entries)
    assert shared_state.get_model_from_transcript(str(f), max_bytes=256) == "claude-early"


def test_get_model_oversized_final_line_falls_back(tmp_path):
    # (codex) The FINAL record is larger than the window (a big tool result /
    # paste). The tail holds only a fragment of it -> no model -> the full-file
    # fallback returns the model on the line before it.
    f = tmp_path / "t.jsonl"
    _write_jsonl(f, [
        _model_entry("claude-before"),
        {"type": "user", "message": {"role": "user", "content": "z" * 4000}},
    ])
    assert shared_state.get_model_from_transcript(str(f), max_bytes=512) == "claude-before"


def test_get_model_no_model_anywhere_even_with_fallback(tmp_path):
    # Fallback that finds nothing still returns "unknown".
    f = tmp_path / "t.jsonl"
    entries = [{"type": "user", "message": {"role": "user", "content": "x" * 200}} for _ in range(50)]
    _write_jsonl(f, entries)
    assert shared_state.get_model_from_transcript(str(f), max_bytes=256) == "unknown"


def test_get_model_flat_runtime_on_50mb(tmp_path):
    """Model detection stays flat w.r.t. transcript size. Build a ~50MB JSONL
    whose newest model entry is the LAST line; assert it is returned AND the call
    completes quickly (bounded tail-read never touches the 50MB prefix)."""
    f = tmp_path / "big.jsonl"
    filler = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 900}}) + "\n"
    block = filler * 1000  # ~0.95 MB
    with open(f, "w", encoding="utf-8") as fh:
        for _ in range(56):  # ~53 MB, comfortably over 50 MiB
            fh.write(block)
        fh.write(json.dumps(_model_entry("claude-fast")) + "\n")
    assert f.stat().st_size > 50 * 1024 * 1024

    start = time.monotonic()
    got = shared_state.get_model_from_transcript(str(f))
    elapsed = time.monotonic() - start

    assert got == "claude-fast", got
    assert elapsed < 3.0, f"tail-read should be flat/fast, took {elapsed:.2f}s"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
