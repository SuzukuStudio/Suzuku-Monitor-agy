#!/usr/bin/env python3
"""Shared transcript parser, settings store, and session scanner for suzuku-monitor."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS: dict[str, Any] = {
    "warnTokens": 80000,
    "dangerTokens": 150000,
    "cacheTtlSec": 300,
    "pollIntervalMs": 2000,
    "desktopNotifyEnabled": True,
    "notifyOnCacheExpiry": True,
    "notifyOnContextGrowth": True,
    "notifyOnTurnEnd": False,
    "notifyBeforeExpiry": True,
    "remoteWebhookUrl": "",
    "remoteWebhookToken": "",
    "chatWarningOnDanger": False,
}

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_PBTXT_TITLE_RE = re.compile(r'(?:^|\s)title\s*:\s*"((?:[^"\\]|\\.)*)"')
_PBTXT_ESC_RE = re.compile(r'\\([0-7]{1,3}|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)')


def get_brain_dir() -> Path:
    """Return the Antigravity brain directory path."""
    env_dir = os.environ.get("ANTIGRAVITY_BRAIN_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".gemini" / "antigravity" / "brain"


def get_default_data_dir() -> Path:
    """Return fallback persistent data directory if PLUGIN_DATA is unset."""
    env_dir = os.environ.get("PLUGIN_DATA")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".gemini" / "config" / "plugins" / "suzuku-monitor" / ".data"


def get_model_context_limit(model_name: str | None) -> int:
    """Return approximate context window size in tokens for a model name."""
    lower = (model_name or "").lower()
    if "claude" in lower:
        return 200_000
    if "gpt-oss" in lower:
        return 128_000
    if "neon" in lower:
        return 512_000
    return 1_000_000


def _read_json_file(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk, returning {} if missing or invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_atomic(path: Path, data: Any) -> None:
    """Atomically write JSON data to path, creating parent directories if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _clamp_int(val: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(val)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def _coerce_bool(src: dict[str, Any], key: str, fallback: bool) -> bool:
    if key not in src:
        return fallback
    val = src[key]
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def _normalize_webhook_url(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    url = raw.strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return ""


def normalize_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Merge partial settings with defaults and clamp numeric bounds."""
    src = raw or {}
    warn = _clamp_int(src.get("warnTokens"), 5_000, 900_000, DEFAULT_SETTINGS["warnTokens"])
    danger = _clamp_int(src.get("dangerTokens"), 10_000, 950_000, DEFAULT_SETTINGS["dangerTokens"])
    if danger <= warn:
        danger = min(950_000, warn + 10_000)
    token = src.get("remoteWebhookToken")
    token_str = token.strip() if isinstance(token, str) else DEFAULT_SETTINGS["remoteWebhookToken"]
    return {
        "warnTokens": warn,
        "dangerTokens": danger,
        "cacheTtlSec": _clamp_int(src.get("cacheTtlSec"), 30, 86_400, DEFAULT_SETTINGS["cacheTtlSec"]),
        "pollIntervalMs": _clamp_int(src.get("pollIntervalMs"), 1000, 60_000, DEFAULT_SETTINGS["pollIntervalMs"]),
        "desktopNotifyEnabled": _coerce_bool(src, "desktopNotifyEnabled", DEFAULT_SETTINGS["desktopNotifyEnabled"]),
        "notifyOnCacheExpiry": _coerce_bool(src, "notifyOnCacheExpiry", DEFAULT_SETTINGS["notifyOnCacheExpiry"]),
        "notifyOnContextGrowth": _coerce_bool(src, "notifyOnContextGrowth", DEFAULT_SETTINGS["notifyOnContextGrowth"]),
        "notifyOnTurnEnd": _coerce_bool(src, "notifyOnTurnEnd", DEFAULT_SETTINGS["notifyOnTurnEnd"]),
        "notifyBeforeExpiry": _coerce_bool(src, "notifyBeforeExpiry", DEFAULT_SETTINGS["notifyBeforeExpiry"]),
        "remoteWebhookUrl": _normalize_webhook_url(src.get("remoteWebhookUrl")),
        "remoteWebhookToken": token_str,
        "chatWarningOnDanger": _coerce_bool(src, "chatWarningOnDanger", DEFAULT_SETTINGS["chatWarningOnDanger"]),
    }


def load_settings(data_dir: str | Path) -> dict[str, Any]:
    """Load persisted settings merged with defaults."""
    path = Path(data_dir) / "settings.json"
    return normalize_settings(_read_json_file(path))


def save_settings(data_dir: str | Path, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge patch into settings, persist atomically, and return normalized settings."""
    current = load_settings(data_dir)
    merged = {**current, **{k: v for k, v in (patch or {}).items() if k in DEFAULT_SETTINGS}}
    normalized = normalize_settings(merged)
    _write_json_atomic(Path(data_dir) / "settings.json", normalized)
    return normalized


def load_active_session(data_dir: str | Path) -> dict[str, Any]:
    """Load the most recently hooked active session metadata."""
    return _read_json_file(Path(data_dir) / "active-session.json")


def save_active_session(data_dir: str | Path, info: dict[str, Any]) -> None:
    """Persist active session metadata atomically."""
    _write_json_atomic(Path(data_dir) / "active-session.json", info)


def _parse_iso_timestamp(ts: str | None) -> float | None:
    """Parse an ISO-8601 timestamp string to UTC POSIX seconds."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        normalized = ts.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _extract_conversation_id(path: Path) -> str:
    """Extract conversationId from .../brain/<conv_id>/.system_generated/logs/transcript.jsonl."""
    parts = path.resolve().parts
    for i, part in enumerate(parts):
        if part == ".system_generated" and i >= 1:
            return parts[i - 1]
    return path.parent.stem or path.stem


def _resolve_conv_dir(path: Path) -> Path | None:
    """Resolve .../brain/<conv_id> from a transcript path under .system_generated."""
    parts = path.resolve().parts
    for i, part in enumerate(parts):
        if part == ".system_generated" and i >= 1:
            return Path(*parts[:i])
    return None


def _is_safe_member_conv_id(conv_id: str) -> bool:
    """Return True if conv_id is a single non-traversing path component."""
    return bool(conv_id) and conv_id not in (".", "..") and "/" not in conv_id and "\\" not in conv_id


def _extract_title(content: str) -> str:
    """Extract a concise display title from the first USER_INPUT content."""
    if not isinstance(content, str):
        return "Untitled session"
    match = _USER_REQUEST_RE.search(content)
    text = match.group(1) if match else content
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "Untitled session"
    if len(text) > 64:
        return text[:61] + "..."
    return text


def _resolve_antigravity_root_from_transcript(path: Path) -> Path:
    """Resolve the Antigravity data root directory from a transcript path."""
    resolved = path.resolve()
    parts = resolved.parts
    for idx in range(len(parts) - 1, -1, -1):
        if parts[idx] == "brain" and idx >= 1:
            return Path(*parts[:idx])
    return get_brain_dir().parent


def _normalize_display_title(raw: str | None) -> str | None:
    """Normalize and truncate a raw title string, returning None if blank."""
    if not isinstance(raw, str):
        return None
    text = re.sub(r"\s+", " ", raw).strip()
    if not text:
        return None
    if len(text) > 64:
        return text[:61] + "..."
    return text


def _unescape_pbtxt_string(raw: str) -> str:
    """Decode protobuf text format string escapes into UTF-8 text."""
    simple_map = {
        "a": b"\x07",
        "b": b"\x08",
        "f": b"\x0c",
        "n": b"\n",
        "r": b"\r",
        "t": b"\t",
        "v": b"\x0b",
        "\\": b"\\",
        '"': b'"',
        "'": b"'",
    }
    out = bytearray()
    pos = 0
    for m in _PBTXT_ESC_RE.finditer(raw):
        if m.start() > pos:
            out.extend(raw[pos : m.start()].encode("utf-8"))
        tok = m.group(1)
        if tok in simple_map:
            out.extend(simple_map[tok])
        elif tok.startswith("x"):
            out.append(int(tok[1:], 16))
        elif tok.startswith("u"):
            out.extend(chr(int(tok[1:], 16)).encode("utf-8"))
        elif all(ch in "01234567" for ch in tok):
            out.append(int(tok, 8) & 0xFF)
        else:
            out.extend(tok.encode("utf-8"))
        pos = m.end()
    if pos < len(raw):
        out.extend(raw[pos:].encode("utf-8"))
    return out.decode("utf-8", errors="replace")


def _read_annotation_title(antigravity_root: Path, conv_id: str) -> str | None:
    """Read title from annotations/<conv_id>.pbtxt if present."""
    if not conv_id:
        return None
    ann_path = antigravity_root / "annotations" / f"{conv_id}.pbtxt"
    try:
        text = ann_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _PBTXT_TITLE_RE.search(text)
    if not m:
        return None
    return _normalize_display_title(_unescape_pbtxt_string(m.group(1)))


def _load_db_titles(
    antigravity_root: Path,
    conv_ids: list[str] | None = None,
) -> dict[str, tuple[str | None, str | None]]:
    """Read (title, preview) pairs from conversation_summaries.db in read-only mode."""
    db_path = antigravity_root / "conversation_summaries.db"
    if not db_path.is_file():
        return {}
    if conv_ids is not None and len(conv_ids) == 0:
        return {}

    con: sqlite3.Connection | None = None
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=0.2)
        cur = con.cursor()
        if conv_ids is None:
            rows = cur.execute("SELECT conversation_id, title, preview FROM conversation_summaries").fetchall()
        else:
            placeholders = ",".join("?" for _ in conv_ids)
            rows = cur.execute(
                f"SELECT conversation_id, title, preview FROM conversation_summaries WHERE conversation_id IN ({placeholders})",
                conv_ids,
            ).fetchall()
        result: dict[str, tuple[str | None, str | None]] = {}
        for cid, t_raw, p_raw in rows:
            if cid:
                result[str(cid)] = (_normalize_display_title(t_raw), _normalize_display_title(p_raw))
        return result
    except (sqlite3.Error, OSError, ValueError):
        return {}
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


def resolve_app_titles(antigravity_root: Path, conv_ids: list[str]) -> dict[str, str]:
    """Resolve Antigravity display titles by priority: DB title > annotation > DB preview."""
    unique_ids = list(dict.fromkeys(cid for cid in conv_ids if cid))
    if not unique_ids:
        return {}
    db_titles = _load_db_titles(antigravity_root, unique_ids)
    resolved: dict[str, str] = {}
    for cid in unique_ids:
        db_title, db_preview = db_titles.get(cid, (None, None))
        if db_title is not None:
            resolved[cid] = db_title
            continue
        ann_title = _read_annotation_title(antigravity_root, cid)
        if ann_title is not None:
            resolved[cid] = ann_title
            continue
        if db_preview is not None:
            resolved[cid] = db_preview
    return resolved


def _extract_tool_names(raw_calls: Any) -> list[str]:
    if not isinstance(raw_calls, list):
        return []
    names: list[str] = []
    for item in raw_calls:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def _infer_model_role(
    model_name: str,
    planner_models: set[str],
    executor_models: set[str],
    is_orchestrator_hint: bool,
) -> str:
    """Infer Orchestrator role ('planner', 'executor', or 'primary') for a model."""
    if model_name in planner_models:
        return "planner"
    if model_name in executor_models:
        return "executor"
    if is_orchestrator_hint or model_name:
        lower = model_name.lower()
        if any(k in lower for k in ("hydrogen", "sonnet", "flash")):
            return "executor"
        if any(k in lower for k in ("neon", "carbon", "argon", "opus")):
            return "planner"
    return "primary"


def _stat_swarm_files(swarm_dir: Path) -> tuple[int, int]:
    """Return (max_mtime_ns, total_bytes) across core swarm metadata files."""
    max_mtime_ns = 0
    total_size = 0
    for fname in ("members.json", "board.jsonl", "swarm.json", "plan.json"):
        fpath = swarm_dir / fname
        try:
            st = fpath.stat()
        except OSError:
            continue
        if st.st_mtime_ns > max_mtime_ns:
            max_mtime_ns = st.st_mtime_ns
        total_size += st.st_size
    return max_mtime_ns, total_size


def _parse_worker_transcript(worker_tp: Path) -> dict[str, Any]:
    """Read lightweight status and token totals from a swarm worker's transcript."""
    result: dict[str, Any] = {
        "hasEntries": False,
        "lastEntryType": None,
        "hasModelInvocation": False,
        "lastModelToolCalls": [],
        "lastModel": "",
        "calls": 0,
        "contextTokens": 0,
        "uncachedInput": 0,
        "cacheRead": 0,
        "output": 0,
        "mtimeNs": 0,
        "sizeBytes": 0,
    }
    try:
        st = worker_tp.stat()
    except OSError:
        return result

    result["mtimeNs"] = st.st_mtime_ns
    result["sizeBytes"] = st.st_size

    try:
        with worker_tp.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue

                result["hasEntries"] = True
                entry_type = entry.get("type")
                result["lastEntryType"] = str(entry_type) if entry_type is not None else None
                if entry_type == "USER_INPUT":
                    continue
                if entry_type == "CHECKPOINT" or (
                    entry.get("source") == "SYSTEM" and not entry.get("model")
                ):
                    continue

                has_tokens = isinstance(entry.get("input_tokens"), int) or isinstance(
                    entry.get("cache_read_tokens"), int
                )
                if not has_tokens and entry_type != "PLANNER_RESPONSE":
                    continue

                result["hasModelInvocation"] = True
                result["lastModelToolCalls"] = _extract_tool_names(entry.get("tool_calls"))
                m_name = str(entry.get("model") or "")
                if m_name:
                    result["lastModel"] = m_name

                if has_tokens:
                    inp = int(entry.get("input_tokens") or 0)
                    cr = int(entry.get("cache_read_tokens") or 0)
                    out = int(entry.get("output_tokens") or 0)
                    result["calls"] += 1
                    result["contextTokens"] = inp + cr
                    result["uncachedInput"] += inp
                    result["cacheRead"] += cr
                    result["output"] += out
    except OSError:
        return result

    return result


def _infer_worker_status(member: dict[str, Any], worker_metrics: dict[str, Any]) -> str:
    """Infer swarm worker status ('killed', 'stopped', 'working', or 'waiting')."""
    desired = str(member.get("desired_state") or "").lower()
    phase = str(member.get("phase") or "").lower()
    if bool(member.get("killed")) or desired == "killed" or phase == "killed":
        return "killed"
    if desired == "stopped" or phase == "stopped":
        return "stopped"
    if worker_metrics.get("hasEntries"):
        if worker_metrics.get("lastEntryType") == "USER_INPUT":
            return "working"
        if worker_metrics.get("hasModelInvocation"):
            tool_calls = worker_metrics.get("lastModelToolCalls") or []
            if not tool_calls or "swarm_wait" in tool_calls:
                return "waiting"
            return "working"
    return "working" if str(member.get("wake_mode") or "").lower() == "working" else "waiting"


def _parse_swarm_info(conv_dir: Path, conv_id: str) -> tuple[bool, dict[str, Any] | None, int, int]:
    """Parse read-only .system_generated/swarm/ state if conv_dir is a Swarm coordinator."""
    swarm_dir = conv_dir / ".system_generated" / "swarm"
    members_path = swarm_dir / "members.json"
    swarm_json_path = swarm_dir / "swarm.json"
    if not (members_path.is_file() or swarm_json_path.is_file()):
        return False, None, 0, 0

    members_data = _read_json_file(members_path)
    raw_title = members_data.get("title")
    swarm_title = re.sub(r"\s+", " ", raw_title).strip() if isinstance(raw_title, str) else ""
    if not swarm_title:
        plan_data = _read_json_file(swarm_dir / "plan.json")
        goal = plan_data.get("goal")
        if isinstance(goal, dict) and isinstance(goal.get("title"), str):
            swarm_title = re.sub(r"\s+", " ", goal["title"]).strip()

    board_messages = 0
    board_path = swarm_dir / "board.jsonl"
    try:
        with board_path.open("r", encoding="utf-8", errors="replace") as bf:
            for raw_line in bf:
                s = raw_line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    board_messages += 1
    except OSError:
        pass

    max_mtime_ns, total_size = _stat_swarm_files(swarm_dir)
    brain_dir = conv_dir.parent
    workers: list[dict[str, Any]] = []
    members_raw = members_data.get("members")
    if isinstance(members_raw, dict):
        for name, m in members_raw.items():
            if not isinstance(m, dict):
                continue
            w_cid = str(m.get("conversation_id") or "").strip()
            if name == "coordinator" or (w_cid and w_cid == conv_id):
                continue

            worker_metrics: dict[str, Any] = {}
            if _is_safe_member_conv_id(w_cid):
                w_tp = brain_dir / w_cid / ".system_generated" / "logs" / "transcript.jsonl"
                if w_tp.is_file():
                    worker_metrics = _parse_worker_transcript(w_tp)
                    max_mtime_ns = max(max_mtime_ns, int(worker_metrics.get("mtimeNs") or 0))
                    total_size += int(worker_metrics.get("sizeBytes") or 0)

            w_model = str(
                m.get("applied_model")
                or m.get("desired_model")
                or m.get("model")
                or worker_metrics.get("lastModel")
                or "unknown"
            )
            w_status = _infer_worker_status(m, worker_metrics)
            workers.append(
                {
                    "name": str(name),
                    "role": str(m.get("role") or "worker"),
                    "model": w_model,
                    "status": w_status,
                    "calls": int(worker_metrics.get("calls") or 0),
                    "contextTokens": int(worker_metrics.get("contextTokens") or 0),
                    "uncachedInput": int(worker_metrics.get("uncachedInput") or 0),
                    "cacheRead": int(worker_metrics.get("cacheRead") or 0),
                    "output": int(worker_metrics.get("output") or 0),
                }
            )

    working_count = sum(1 for w in workers if w["status"] == "working")
    live_worker_count = sum(1 for w in workers if w["status"] not in ("killed", "stopped"))
    retired_worker_count = len(workers) - live_worker_count
    swarm_info = {
        "title": swarm_title,
        "workerCount": len(workers),
        "liveWorkerCount": live_worker_count,
        "retiredWorkerCount": retired_worker_count,
        "workingCount": working_count,
        "boardMessages": board_messages,
        "totals": {
            "calls": sum(w["calls"] for w in workers),
            "uncachedInput": sum(w["uncachedInput"] for w in workers),
            "cacheRead": sum(w["cacheRead"] for w in workers),
            "output": sum(w["output"] for w in workers),
        },
        "workers": workers,
    }
    return True, swarm_info, max_mtime_ns, total_size


def scan_swarm_map(brain_dir: str | Path | None = None) -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
    """Scan brain_dir for swarm coordinators and return (worker_to_coord, coord_activity)."""
    root = Path(brain_dir) if brain_dir is not None else get_brain_dir()
    worker_to_coord: dict[str, str] = {}
    coord_activity: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return worker_to_coord, coord_activity

    for members_path in root.glob("*/.system_generated/swarm/members.json"):
        swarm_dir = members_path.parent
        coord_cid = swarm_dir.parent.parent.name
        members_data = _read_json_file(members_path)
        max_mtime_ns, total_size = _stat_swarm_files(swarm_dir)
        members_raw = members_data.get("members")
        if isinstance(members_raw, dict):
            for name, m in members_raw.items():
                if not isinstance(m, dict):
                    continue
                w_cid = str(m.get("conversation_id") or "").strip()
                if name == "coordinator" or not w_cid or w_cid == coord_cid:
                    continue
                if not _is_safe_member_conv_id(w_cid):
                    continue
                worker_to_coord[w_cid] = coord_cid
                w_tp = root / w_cid / ".system_generated" / "logs" / "transcript.jsonl"
                try:
                    st = w_tp.stat()
                    max_mtime_ns = max(max_mtime_ns, st.st_mtime_ns)
                    total_size += st.st_size
                except OSError:
                    pass
        coord_activity[coord_cid] = (max_mtime_ns, total_size)

    return worker_to_coord, coord_activity


def parse_transcript(
    path: str | Path,
    now_ts: float | None = None,
    resolve_app_title: bool = True,
) -> dict[str, Any]:
    """Parse a transcript.jsonl file into Orchestrator-aware context metrics and history."""
    p = Path(path)
    stat = p.stat()
    now = now_ts if now_ts is not None else time.time()

    conv_id = _extract_conversation_id(p)
    title: str | None = None
    user_turns = 0
    turn_seq = 0
    invocations: list[dict[str, Any]] = []
    last_by_model: dict[str, dict[str, Any]] = {}
    last_turn_by_model: dict[str, int] = {}
    planner_models: set[str] = set()
    executor_models: set[str] = set()
    plan_handed_off_turn = -1
    model_stats: dict[str, dict[str, Any]] = {}
    total_uncached = 0
    total_cache_read = 0
    total_output = 0
    compaction_count = 0
    last_compaction: dict[str, Any] | None = None
    pending_compaction = False

    with p.open("r", encoding="utf-8", errors="replace") as f:
        for idx, raw_line in enumerate(f):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue

            entry_type = entry.get("type")
            if entry_type == "USER_INPUT":
                user_turns += 1
                turn_seq += 1
                if title is None:
                    title = _extract_title(entry.get("content", ""))
                continue

            has_tokens = isinstance(entry.get("input_tokens"), int) or isinstance(
                entry.get("cache_read_tokens"), int
            )
            if not has_tokens:
                continue

            input_tokens = int(entry.get("input_tokens") or 0)
            cache_read_tokens = int(entry.get("cache_read_tokens") or 0)
            output_tokens = int(entry.get("output_tokens") or 0)
            created_at = str(entry.get("created_at") or "")
            step_index = entry.get("step_index", idx)

            if entry_type == "CHECKPOINT" or (
                entry.get("source") == "SYSTEM" and not entry.get("model")
            ):
                total_uncached += input_tokens
                total_cache_read += cache_read_tokens
                total_output += output_tokens
                if entry_type == "CHECKPOINT":
                    compaction_count += 1
                    ts_val = _parse_iso_timestamp(created_at)
                    if ts_val is None:
                        ts_val = stat.st_mtime
                    last_compaction = {
                        "stepIndex": step_index,
                        "createdAt": created_at
                        or datetime.fromtimestamp(ts_val, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "timestampTs": ts_val,
                        "beforeTokens": input_tokens + cache_read_tokens,
                        "summaryTokens": output_tokens,
                    }
                    pending_compaction = True
                    last_by_model.clear()
                    last_turn_by_model.clear()
                    for m_stat in model_stats.values():
                        m_stat["staleAfterCompact"] = True
                continue

            context_tokens = input_tokens + cache_read_tokens
            cache_hit_rate = (cache_read_tokens / context_tokens) if context_tokens > 0 else 0.0
            tool_calls = _extract_tool_names(entry.get("tool_calls"))
            has_tool_calls = bool(tool_calls)
            model = str(entry.get("model") or "")

            if "exit_plan_mode" in tool_calls and model:
                planner_models.add(model)
                plan_handed_off_turn = turn_seq
            elif plan_handed_off_turn == turn_seq and model and model not in planner_models:
                executor_models.add(model)

            prev_overall = invocations[-1] if invocations else None
            prev_same = last_by_model.get(model) if model else None
            model_switch = bool(
                prev_overall and model and prev_overall.get("model") and model != prev_overall["model"]
            )

            prev_model_turn = last_turn_by_model.get(model) if model else None
            is_executor_candidate = bool(
                model
                and (
                    model in executor_models
                    or (any(k in model.lower() for k in ("hydrogen", "sonnet", "flash")) and model not in planner_models)
                )
            )
            handoff_rebuild = bool(
                prev_same is not None
                and model_switch
                and is_executor_candidate
                and prev_model_turn is not None
                and prev_model_turn != turn_seq
            )

            ref_ctx = (
                prev_same["contextTokens"]
                if prev_same is not None
                else (prev_overall["contextTokens"] if prev_overall is not None else 0)
            )
            compacted = bool(
                pending_compaction
                or (
                    ref_ctx >= 20_000
                    and context_tokens < ref_ctx * 0.55
                    and (ref_ctx - context_tokens) >= 10_000
                )
            )
            pending_compaction = False

            # First switch to a different model (or cross-turn Executor handoff rebuild) must not be
            # flagged as an unexpected cache miss.
            cache_miss = bool(
                prev_same is not None
                and not compacted
                and not handoff_rebuild
                and prev_same["contextTokens"] >= 10_000
                and (
                    (cache_read_tokens == 0 and input_tokens >= 10_000)
                    or (cache_hit_rate < 0.2 and input_tokens >= 20_000)
                )
            )

            delta_same = (
                context_tokens - prev_same["contextTokens"]
                if prev_same is not None
                else context_tokens
            )

            total_uncached += input_tokens
            total_cache_read += cache_read_tokens
            total_output += output_tokens

            inv = {
                "stepIndex": step_index,
                "createdAt": created_at,
                "model": model,
                "inputTokens": input_tokens,
                "cacheReadTokens": cache_read_tokens,
                "outputTokens": output_tokens,
                "contextTokens": context_tokens,
                "cacheHitRate": round(cache_hit_rate, 4),
                "toolCalls": tool_calls,
                "hasToolCalls": has_tool_calls,
                "compacted": compacted,
                "cacheMiss": cache_miss,
                "modelSwitch": model_switch,
                "handoffRebuild": handoff_rebuild,
                "deltaFromSameModel": delta_same,
            }
            invocations.append(inv)

            model_key = model or "unknown"
            last_by_model[model_key] = inv
            last_turn_by_model[model_key] = turn_seq
            ts_val = _parse_iso_timestamp(created_at)
            if ts_val is None:
                ts_val = stat.st_mtime
            prev_stat = model_stats.get(model_key)
            calls_count = (prev_stat["calls"] + 1) if prev_stat else 1
            miss_count = (prev_stat["cacheMisses"] if prev_stat else 0) + (1 if cache_miss else 0)
            model_stats[model_key] = {
                "model": model_key,
                "calls": calls_count,
                "cacheMisses": miss_count,
                "contextTokens": context_tokens,
                "inputTokens": input_tokens,
                "cacheReadTokens": cache_read_tokens,
                "cacheHitRate": round(cache_hit_rate, 4),
                "lastActivityAt": created_at
                or datetime.fromtimestamp(ts_val, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lastActivityTs": ts_val,
                "idleSeconds": max(0, int(now - ts_val)),
                "stepIndex": step_index,
                "contextLimit": get_model_context_limit(model_key),
                "staleAfterCompact": False,
            }

    latest = invocations[-1] if invocations else None
    delta_tokens = latest["deltaFromSameModel"] if latest else 0

    model_name = latest["model"] if latest else ""
    context_limit = get_model_context_limit(model_name)

    just_compacted = bool(pending_compaction and last_compaction is not None)
    if just_compacted and last_compaction is not None:
        last_activity_ts = float(last_compaction["timestampTs"])
        last_activity_at = str(last_compaction["createdAt"])
    else:
        last_activity_ts = _parse_iso_timestamp(latest["createdAt"]) if latest else None
        if last_activity_ts is None:
            last_activity_ts = stat.st_mtime
        last_activity_at = (
            latest["createdAt"]
            if (latest and latest.get("createdAt"))
            else datetime.fromtimestamp(last_activity_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    idle_seconds = max(0, int(now - last_activity_ts))

    is_orchestrator = len([m for m in model_stats.values() if m["model"] != "unknown"]) >= 2
    for m_stat in model_stats.values():
        m_stat["role"] = (
            _infer_model_role(str(m_stat["model"]), planner_models, executor_models, True)
            if is_orchestrator
            else "primary"
        )

    role_rank = {"planner": 0, "primary": 1, "executor": 2}
    current_model_key = (model_name or "unknown") if latest else ""
    model_caches = sorted(
        model_stats.values(),
        key=lambda m: (
            1 if m.get("staleAfterCompact") else 0,
            role_rank.get(str(m.get("role") or "primary"), 1),
            -float(m.get("lastActivityTs") or 0),
        ),
    )
    for m in model_caches:
        m["isCurrent"] = bool(
            (not just_compacted)
            and (not m.get("staleAfterCompact"))
            and current_model_key
            and m["model"] == current_model_key
        )

    freshness_target: dict[str, Any] | None = None
    if not just_compacted:
        active_caches = [m for m in model_caches if not m.get("staleAfterCompact")]
        target_mc: dict[str, Any] | None = None
        if is_orchestrator:
            planners = [m for m in active_caches if m.get("role") == "planner"]
            if planners:
                target_mc = planners[0]
            elif active_caches:
                target_mc = max(active_caches, key=lambda m: float(m.get("lastActivityTs") or 0))
        elif active_caches:
            target_mc = active_caches[0]

        if target_mc is not None:
            freshness_target = {
                "model": target_mc["model"],
                "role": target_mc["role"],
                "contextTokens": target_mc["contextTokens"],
                "lastActivityAt": target_mc["lastActivityAt"],
                "lastActivityTs": target_mc["lastActivityTs"],
                "idleSeconds": target_mc["idleSeconds"],
            }

    fallback_title = title or "Untitled session"
    display_title = fallback_title
    if resolve_app_title and conv_id:
        app_root = _resolve_antigravity_root_from_transcript(p)
        resolved = resolve_app_titles(app_root, [conv_id]).get(conv_id)
        if resolved:
            display_title = resolved

    conv_dir = _resolve_conv_dir(p)
    is_swarm = False
    swarm_info: dict[str, Any] | None = None
    swarm_max_mtime_ns = 0
    if conv_dir is not None:
        is_swarm, swarm_info, swarm_max_mtime_ns, _ = _parse_swarm_info(conv_dir, conv_id)
    eff_mtime_ns = max(stat.st_mtime_ns, swarm_max_mtime_ns)

    return {
        "conversationId": conv_id,
        "title": display_title,
        "fallbackTitle": fallback_title,
        "transcriptPath": str(p),
        "model": model_name,
        "contextLimit": context_limit,
        "latest": latest,
        "deltaTokens": delta_tokens,
        "isOrchestrator": is_orchestrator,
        "isSwarm": is_swarm,
        "swarm": swarm_info,
        "modelCaches": model_caches,
        "freshnessTarget": freshness_target,
        "justCompacted": just_compacted,
        "compactionCount": compaction_count,
        "lastCompaction": (
            {k: v for k, v in last_compaction.items() if k != "timestampTs"}
            if last_compaction is not None
            else None
        ),
        "lastActivityAt": last_activity_at,
        "lastActivityTs": last_activity_ts,
        "idleSeconds": idle_seconds,
        "userTurns": user_turns,
        "modelCalls": len(invocations),
        "totals": {
            "uncachedInput": total_uncached,
            "cacheRead": total_cache_read,
            "output": total_output,
        },
        "history": invocations[-40:],
        "mtimeNs": eff_mtime_ns,
        "mtimeMs": int(eff_mtime_ns // 1_000_000),
    }


def _recompute_idle(parsed: dict[str, Any], now_ts: float) -> dict[str, Any]:
    """Return a copy of parsed transcript dict with freshly computed idleSeconds."""
    copy = dict(parsed)
    last_ts = copy.get("lastActivityTs")
    if isinstance(last_ts, (int, float)):
        copy["idleSeconds"] = max(0, int(now_ts - last_ts))
    ft = copy.get("freshnessTarget")
    if isinstance(ft, dict):
        ft_copy = dict(ft)
        fts = ft_copy.get("lastActivityTs")
        if isinstance(fts, (int, float)):
            ft_copy["idleSeconds"] = max(0, int(now_ts - fts))
        copy["freshnessTarget"] = ft_copy
    caches = copy.get("modelCaches")
    if isinstance(caches, list):
        refreshed = []
        for item in caches:
            if isinstance(item, dict):
                mc = dict(item)
                mts = mc.get("lastActivityTs")
                if isinstance(mts, (int, float)):
                    mc["idleSeconds"] = max(0, int(now_ts - mts))
                refreshed.append(mc)
        copy["modelCaches"] = refreshed
    return copy


def list_sessions(
    brain_dir: str | Path | None = None,
    active_info: dict[str, Any] | None = None,
    limit: int = 30,
    cache: dict[str, tuple[int, int, dict[str, Any]]] | None = None,
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    """Scan brain_dir for transcript.jsonl files sorted by most recently modified."""
    root = Path(brain_dir) if brain_dir is not None else get_brain_dir()
    if not root.exists():
        return []

    now = now_ts if now_ts is not None else time.time()
    worker_to_coord, coord_activity = scan_swarm_map(root)
    candidates: list[tuple[int, int, Path]] = []
    for p in root.glob("*/.system_generated/logs/transcript.jsonl"):
        cid = _extract_conversation_id(p)
        if cid in worker_to_coord:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        s_mtime_ns, s_size = coord_activity.get(cid, (0, 0))
        eff_mtime_ns = max(st.st_mtime_ns, s_mtime_ns)
        eff_size = st.st_size + s_size
        candidates.append((eff_mtime_ns, eff_size, p))

    candidates.sort(key=lambda item: item[0], reverse=True)
    candidates = candidates[:limit]

    raw_active_cid = (active_info or {}).get("conversationId")
    active_conv_id = worker_to_coord.get(raw_active_cid, raw_active_cid) if raw_active_cid else None
    sessions: list[dict[str, Any]] = []

    for idx, (mtime_ns, size, p) in enumerate(candidates):
        key = str(p)
        parsed: dict[str, Any] | None = None
        if cache is not None and key in cache:
            c_mtime, c_size, c_parsed = cache[key]
            if c_mtime == mtime_ns and c_size == size:
                parsed = _recompute_idle(c_parsed, now)

        if parsed is None:
            try:
                parsed = parse_transcript(p, now_ts=now, resolve_app_title=False)
            except OSError:
                continue
            if cache is not None:
                cache[key] = (mtime_ns, size, parsed)
            item = dict(parsed)
        else:
            item = parsed

        item["mtimeNs"] = mtime_ns
        item["mtimeMs"] = int(mtime_ns // 1_000_000)
        item["isActive"] = bool(active_conv_id and item["conversationId"] == active_conv_id)
        item["isMostRecent"] = idx == 0
        sessions.append(item)

    conv_ids = [s["conversationId"] for s in sessions if s.get("conversationId")]
    app_titles = resolve_app_titles(root.parent, conv_ids)
    for s in sessions:
        cid = s.get("conversationId") or ""
        s["title"] = app_titles.get(cid) or s.get("fallbackTitle") or s.get("title") or "Untitled session"

    return sessions


def fmt_k(tokens: int | float) -> str:
    """Format token count concisely (e.g., 42.8k or 850)."""
    val = int(tokens)
    if abs(val) >= 1000:
        return f"{val / 1000:.1f}k"
    return str(val)


def fmt_duration(seconds: int) -> str:
    """Format duration in seconds into concise readable text."""
    sec = max(0, int(seconds))
    if sec < 60:
        return f"{sec}s"
    mins, rem = divmod(sec, 60)
    if mins < 60:
        return f"{mins}m{rem:02d}s"
    hours, rem_mins = divmod(mins, 60)
    return f"{hours}h{rem_mins:02d}m"
