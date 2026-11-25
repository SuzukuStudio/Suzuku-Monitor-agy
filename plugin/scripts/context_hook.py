#!/usr/bin/env python3
"""Lifecycle hook for suzuku-monitor: syncs active session and dispatches desktop/remote alerts."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from notifier import maybe_notify_from_parsed  # noqa: E402
from transcript_parser import (  # noqa: E402
    _read_json_file,
    _write_json_atomic,
    fmt_duration,
    fmt_k,
    get_default_data_dir,
    load_settings,
    parse_transcript,
    save_active_session,
)


def resolve_lightweight_transcript(transcript_path: str) -> str:
    """Prefer compact transcript.jsonl over transcript_full.jsonl when present in the same directory."""
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    if p.name == "transcript_full.jsonl":
        compact = p.with_name("transcript.jsonl")
        if compact.is_file():
            return str(compact)
    return transcript_path


def _maybe_chat_warning(
    data_dir: Path,
    parsed: dict[str, Any],
    settings: dict[str, Any],
    event_name: str,
    invocation_num: int,
) -> dict[str, Any]:
    """Optional opt-in plain systemMessage alert for Remote Control mobile visibility."""
    if parsed.get("justCompacted"):
        return {}
    if not settings.get("chatWarningOnDanger", False):
        return {}
    if event_name != "PreInvocation" or invocation_num != 0:
        return {}

    latest = parsed.get("latest")
    if not latest:
        return {}

    ctx = int(latest.get("contextTokens") or 0)
    warn_tokens = int(settings.get("warnTokens", 80_000))
    danger_tokens = int(settings.get("dangerTokens", 150_000))
    cache_ttl = int(settings.get("cacheTtlSec", 300))
    ft = parsed.get("freshnessTarget") if isinstance(parsed.get("freshnessTarget"), dict) else None
    idle_sec = int((ft.get("idleSeconds") if ft else parsed.get("idleSeconds")) or 0)
    stale_ctx = int((ft.get("contextTokens") if ft else ctx) or 0)
    stale_model = str((ft.get("model") if ft else latest.get("model")) or "unknown")
    cache_miss = bool(latest.get("cacheMiss"))
    notify_cache = bool(settings.get("notifyOnCacheExpiry", True))
    notify_ctx = bool(settings.get("notifyOnContextGrowth", True))

    is_stale = notify_cache and idle_sec >= cache_ttl and stale_ctx >= int(warn_tokens * 0.6)
    is_cache_miss = notify_cache and cache_miss
    is_ctx_danger = notify_ctx and ctx >= danger_tokens
    is_danger = is_ctx_danger or is_cache_miss
    if not (is_stale or is_danger):
        return {}

    conv_id = str(parsed.get("conversationId") or "default")
    state_path = data_dir / "hook-state.json"
    state = _read_json_file(state_path)
    prev = state.get(conv_id) if isinstance(state.get(conv_id), dict) else {}
    now = int(time.time())
    step_idx = int(latest.get("stepIndex") or 0)

    if prev.get("lastChatWarnStep") == step_idx and (now - int(prev.get("lastChatWarnTs") or 0)) < 120:
        return {}

    reason = "Cache Miss" if is_cache_miss else ("TTL超過" if is_stale else "肥大化")
    shown_ctx = stale_ctx if (is_stale and not is_danger) else ctx
    shown_model = stale_model if (is_stale and not is_danger) else (latest.get("model") or "unknown")
    msg = (
        f"[Suzuku Monitor] {reason}: {fmt_k(shown_ctx)} tokens"
        f" (idle {fmt_duration(idle_sec)}, model {shown_model}). "
        "Consider /compact."
    )
    state[conv_id] = {"lastChatWarnStep": step_idx, "lastChatWarnTs": now}
    _write_json_atomic(state_path, state)

    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": msg,
                }
            }
        ],
        "terminationBehavior": "",
    }


def handle_hook(payload: dict[str, Any]) -> dict[str, Any]:
    """Sync active session metadata, dispatch notifications, and optionally emit chat alert."""
    data_dir = Path(os.environ.get("PLUGIN_DATA") or get_default_data_dir())
    event_name = str(payload.get("hookEventName") or "")
    conv_id = str(payload.get("conversationId") or "")
    transcript_path = resolve_lightweight_transcript(str(payload.get("transcriptPath") or ""))
    model_name = str(payload.get("modelName") or "")
    workspace_paths = payload.get("workspacePaths") or []
    invocation_num = int(payload.get("invocationNum") or 0)

    if conv_id and transcript_path:
        save_active_session(
            data_dir,
            {
                "conversationId": conv_id,
                "transcriptPath": transcript_path,
                "modelName": model_name,
                "workspacePaths": workspace_paths,
                "updatedAt": int(time.time()),
            },
        )

    settings = load_settings(data_dir)
    need_parse = bool(
        settings.get("desktopNotifyEnabled", True)
        or settings.get("remoteWebhookUrl")
        or settings.get("chatWarningOnDanger", False)
    )
    if not need_parse or not transcript_path:
        return {}

    tp = Path(transcript_path)
    if not tp.is_file():
        return {}

    parsed = parse_transcript(tp)
    if settings.get("desktopNotifyEnabled", True) or settings.get("remoteWebhookUrl"):
        maybe_notify_from_parsed(
            data_dir=data_dir,
            parsed=parsed,
            settings=settings,
            event_name=event_name,
            invocation_num=invocation_num,
        )

    return _maybe_chat_warning(data_dir, parsed, settings, event_name, invocation_num)


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        result = handle_hook(payload)
        sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    except Exception:
        sys.stdout.write("{}\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
