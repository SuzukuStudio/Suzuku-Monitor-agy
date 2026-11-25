#!/usr/bin/env python3
"""Desktop and Remote Control webhook notifier for suzuku-monitor."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from transcript_parser import (  # noqa: E402
    _read_json_file,
    _write_json_atomic,
    fmt_duration,
    fmt_k,
    get_model_context_limit,
)

DEFAULT_ICON_PATH = _SCRIPT_DIR.parent / "assets" / "logo.svg"
REMOTE_CONTROL_URL = "https://antigravity.google.com"


def _build_desktop_env() -> dict[str, str]:
    """Ensure XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS are populated on POSIX."""
    env = dict(os.environ)
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return env
    uid = getuid()
    runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        bus_path = Path(runtime_dir) / "bus"
        if bus_path.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"
    return env


def _popen_detached(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    stdin: int | None = subprocess.DEVNULL,
) -> subprocess.Popen[Any]:
    """Launch a detached background subprocess across POSIX and Windows."""
    kwargs: dict[str, Any] = {
        "stdin": stdin,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if env is not None:
        kwargs["env"] = env
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _replace_id_for(key: str) -> str:
    """Compute a deterministic positive 31-bit integer replace-id for notify-send."""
    val = zlib.crc32(key.encode("utf-8")) & 0x7FFFFFFF
    return str(val or 1)


def _run_notification_cmd(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    wait: bool = False,
) -> tuple[bool, str]:
    """Execute a desktop notification command synchronously or detached."""
    try:
        if wait:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if proc.returncode == 0:
                return True, "ok"
            detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
            return False, detail
        _popen_detached(cmd, env=env)
        return True, "dispatched"
    except Exception as exc:
        return False, str(exc)


def send_desktop_notification(
    title: str,
    body: str,
    urgency: str = "normal",
    replace_key: str | None = None,
    icon_path: str | Path | None = None,
    wait: bool = False,
) -> tuple[bool, str]:
    """Send a desktop notification on Linux (notify-send), macOS (osascript), or Windows (Toast)."""
    if sys.platform == "darwin":
        binary = shutil.which("osascript")
        if not binary:
            return False, "osascript not found"
        cmd = [
            binary,
            "-e",
            "on run argv",
            "-e",
            "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e",
            "end run",
            "--",
            title,
            body,
        ]
        return _run_notification_cmd(cmd, wait=wait)

    if sys.platform == "win32":
        binary = shutil.which("powershell") or shutil.which("pwsh")
        if not binary:
            return False, "powershell not found"
        ps_script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null; "
            "$t = $env:CTX_MON_TITLE; $b = $env:CTX_MON_BODY; "
            "$xmlStr = '<toast><visual><binding template=\"ToastGeneric\"><text>' + $t + '</text><text>' + $b + '</text></binding></visual></toast>'; "
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; $xml.LoadXml($xmlStr); "
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Suzuku Monitor').Show($toast)"
        )
        win_env = dict(os.environ)
        win_env["CTX_MON_TITLE"] = xml_escape(title)
        win_env["CTX_MON_BODY"] = xml_escape(body)
        cmd = [binary, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script]
        return _run_notification_cmd(cmd, env=win_env, wait=wait)

    binary = shutil.which("notify-send")
    if not binary:
        return False, "notify-send not found"

    safe_urgency = urgency if urgency in ("low", "normal", "critical") else "normal"
    cmd = [
        binary,
        "--app-name=Suzuku Monitor",
        f"--urgency={safe_urgency}",
    ]
    icon = Path(icon_path) if icon_path else DEFAULT_ICON_PATH
    if icon.is_file():
        cmd.append(f"--icon={icon}")
    if replace_key:
        cmd.append(f"--replace-id={_replace_id_for(replace_key)}")
    cmd.extend([title, body])

    return _run_notification_cmd(cmd, env=_build_desktop_env(), wait=wait)


def _ascii_header_title(title: str, urgency: str) -> str:
    """Return an ASCII/latin-1 safe HTTP header title for ntfy."""
    ascii_only = title.encode("ascii", "ignore").decode("ascii").strip()
    if ascii_only:
        return f"Suzuku Monitor: {ascii_only}"[:80]
    if urgency == "critical":
        return "Suzuku Monitor Alert"
    if urgency == "normal":
        return "Suzuku Monitor Notice"
    return "Suzuku Monitor Status"


def build_webhook_request(
    url: str,
    title: str,
    body: str,
    urgency: str = "normal",
    token: str = "",
    metadata: dict[str, Any] | None = None,
) -> tuple[str, dict[str, str], bytes]:
    """Build (url, headers, body_bytes) tailored to ntfy, Discord, Slack, or generic JSON webhooks."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()

    headers: dict[str, str] = {
        "User-Agent": "suzuku-monitor-agy/1.2.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    is_discord = host in ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com") and (
        "/api/webhooks/" in path
    )
    is_slack = host == "hooks.slack.com"
    is_ntfy = host == "ntfy.sh" or "ntfy" in host or "/ntfy" in path

    if is_discord:
        headers["Content-Type"] = "application/json; charset=utf-8"
        payload = {"content": f"**{title}**\n{body}"}
        return url, headers, json.dumps(payload, ensure_ascii=False).encode("utf-8")

    if is_slack:
        headers["Content-Type"] = "application/json; charset=utf-8"
        payload = {"text": f"*{title}*\n{body}"}
        return url, headers, json.dumps(payload, ensure_ascii=False).encode("utf-8")

    if is_ntfy:
        priority_map = {"critical": "urgent", "normal": "high", "low": "low"}
        tags_map = {"critical": "rotating_light", "normal": "warning", "low": "bar_chart"}
        headers["Content-Type"] = "text/plain; charset=utf-8"
        headers["Title"] = _ascii_header_title(title, urgency)
        headers["Priority"] = priority_map.get(urgency, "default")
        headers["Tags"] = tags_map.get(urgency, "bell")
        headers["Click"] = REMOTE_CONTROL_URL
        return url, headers, f"{title}\n{body}".encode("utf-8")

    headers["Content-Type"] = "application/json; charset=utf-8"
    generic_payload: dict[str, Any] = {
        "title": title,
        "body": body,
        "urgency": urgency,
        "clickUrl": REMOTE_CONTROL_URL,
    }
    if metadata:
        generic_payload.update(metadata)
    return url, headers, json.dumps(generic_payload, ensure_ascii=False).encode("utf-8")


def _send_webhook_sync(
    url: str,
    title: str,
    body: str,
    urgency: str = "normal",
    token: str = "",
    metadata: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    target_url, headers, data = build_webhook_request(
        url=url,
        title=title,
        body=body,
        urgency=urgency,
        token=token,
        metadata=metadata,
    )
    req = urllib.request.Request(target_url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True, f"HTTP {getattr(resp, 'status', 200)}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def send_remote_webhook(
    url: str,
    title: str,
    body: str,
    urgency: str = "normal",
    token: str = "",
    metadata: dict[str, Any] | None = None,
    wait: bool = False,
) -> tuple[bool, str]:
    """Send a push notification to a configured webhook URL (non-blocking unless wait=True)."""
    clean_url = (url or "").strip()
    if not clean_url.startswith(("http://", "https://")):
        return False, "invalid or empty webhook URL"

    if wait:
        return _send_webhook_sync(clean_url, title, body, urgency, token, metadata)

    job = {
        "url": clean_url,
        "title": title,
        "body": body,
        "urgency": urgency,
        "token": token,
        "metadata": metadata or {},
    }
    try:
        proc = _popen_detached(
            [sys.executable, str(Path(__file__).resolve()), "--webhook-stdin"],
            stdin=subprocess.PIPE,
        )
        if proc.stdin is not None:
            proc.stdin.write(json.dumps(job, ensure_ascii=False).encode("utf-8"))
            proc.stdin.close()
        return True, "dispatched"
    except Exception as exc:
        return False, str(exc)


def dispatch_notification(
    settings: dict[str, Any],
    title: str,
    body: str,
    urgency: str = "normal",
    replace_key: str | None = None,
    icon_path: str | Path | None = None,
    metadata: dict[str, Any] | None = None,
    wait: bool = False,
) -> dict[str, tuple[bool, str]]:
    """Dispatch enabled desktop and remote webhook notifications."""
    results: dict[str, tuple[bool, str]] = {}
    if settings.get("desktopNotifyEnabled", True):
        results["desktop"] = send_desktop_notification(
            title=title,
            body=body,
            urgency=urgency,
            replace_key=replace_key,
            icon_path=icon_path,
            wait=wait,
        )
    webhook_url = str(settings.get("remoteWebhookUrl") or "").strip()
    if webhook_url:
        results["webhook"] = send_remote_webhook(
            url=webhook_url,
            title=title,
            body=body,
            urgency=urgency,
            token=str(settings.get("remoteWebhookToken") or ""),
            metadata=metadata,
            wait=wait,
        )
    return results


def _load_notify_state(data_dir: Path) -> dict[str, Any]:
    state = _read_json_file(data_dir / "notify-state.json")
    if not isinstance(state.get("conversations"), dict):
        state["conversations"] = {}
    if not isinstance(state.get("preExpiryNotified"), dict):
        state["preExpiryNotified"] = {}
    return state


def _save_notify_state(data_dir: Path, state: dict[str, Any]) -> None:
    convs = state.get("conversations", {})
    if len(convs) > 100:
        trimmed = sorted(
            convs.items(),
            key=lambda kv: kv[1].get("updatedAt", 0) if isinstance(kv[1], dict) else 0,
            reverse=True,
        )[:100]
        state["conversations"] = dict(trimmed)

    pre_exp = state.get("preExpiryNotified", {})
    if len(pre_exp) > 200:
        trimmed_pe = sorted(pre_exp.items(), key=lambda kv: kv[1], reverse=True)[:200]
        state["preExpiryNotified"] = dict(trimmed_pe)

    _write_json_atomic(data_dir / "notify-state.json", state)


def maybe_notify_from_parsed(
    data_dir: str | Path,
    parsed: dict[str, Any],
    settings: dict[str, Any],
    event_name: str,
    invocation_num: int = 0,
    icon_path: str | Path | None = None,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """Evaluate hook event against parsed transcript and dispatch notifications with dedup/cooldown."""
    if parsed.get("justCompacted"):
        return None
    latest = parsed.get("latest")
    if not latest:
        return None

    has_channel = bool(settings.get("desktopNotifyEnabled", True) or settings.get("remoteWebhookUrl"))
    if not has_channel:
        return None

    now = now_ts if now_ts is not None else time.time()
    data_path = Path(data_dir)
    state = _load_notify_state(data_path)
    conv_id = str(parsed.get("conversationId") or "default")
    conv_state = dict(state["conversations"].get(conv_id) or {})

    ctx = int(latest.get("contextTokens") or 0)
    limit = int(parsed.get("contextLimit") or 1_000_000)
    pct_limit = (ctx / limit * 100.0) if limit > 0 else 0.0
    warn_tokens = int(settings.get("warnTokens", 80_000))
    danger_tokens = int(settings.get("dangerTokens", 150_000))
    cache_ttl = int(settings.get("cacheTtlSec", 300))
    model_label = str(latest.get("model") or parsed.get("model") or "unknown")
    session_title = str(parsed.get("title") or "Untitled session")
    step_idx = int(latest.get("stepIndex") or 0)
    cache_pct = round(float(latest.get("cacheHitRate") or 0.0) * 100)
    notify_cache = bool(settings.get("notifyOnCacheExpiry", True))
    notify_ctx = bool(settings.get("notifyOnContextGrowth", True))

    chosen: dict[str, str] | None = None

    if event_name == "PostInvocation":
        last_step = int(conv_state.get("lastNotifiedStep", -1))
        if step_idx <= last_step:
            return None

        if notify_ctx and ctx < int(warn_tokens * 0.8):
            conv_state["warnNotified"] = False

        if notify_cache and latest.get("cacheMiss"):
            chosen = {
                "urgency": "critical",
                "title": "🔥 Cache Miss 検知",
                "body": (
                    f"{fmt_k(ctx)} ({pct_limit:.1f}%) · Cache {cache_pct}% · "
                    f"{model_label} · /compact 推奨 ({session_title})"
                ),
                "kind": "cache_miss",
            }
        elif notify_ctx and ctx >= danger_tokens:
            last_danger_ts = float(conv_state.get("lastDangerTs") or 0)
            last_danger_ctx = int(conv_state.get("lastDangerCtx") or 0)
            if (now - last_danger_ts) >= 180 or (ctx - last_danger_ctx) >= 25_000:
                chosen = {
                    "urgency": "critical",
                    "title": "🔴 コンテキスト肥大 (Compact推奨)",
                    "body": (
                        f"{fmt_k(ctx)} / {fmt_k(limit)} ({pct_limit:.1f}%) · Cache {cache_pct}% · "
                        f"{model_label} · /compact 推奨 ({session_title})"
                    ),
                    "kind": "danger",
                }
                conv_state["lastDangerTs"] = now
                conv_state["lastDangerCtx"] = ctx
        elif notify_ctx and ctx >= warn_tokens and not conv_state.get("warnNotified"):
            chosen = {
                "urgency": "normal",
                "title": "🟡 コンテキスト注意",
                "body": (
                    f"{fmt_k(ctx)} ({pct_limit:.1f}%) · Cache {cache_pct}% · "
                    f"{model_label} ({session_title})"
                ),
                "kind": "warn",
            }
            conv_state["warnNotified"] = True
        elif settings.get("notifyOnTurnEnd") and not latest.get("hasToolCalls"):
            chosen = {
                "urgency": "low",
                "title": "📊 ターン完了",
                "body": (
                    f"{fmt_k(ctx)} ({pct_limit:.1f}%) · Cache {cache_pct}% · "
                    f"{model_label} ({session_title})"
                ),
                "kind": "turn_end",
            }

        conv_state["lastNotifiedStep"] = step_idx
        conv_state["updatedAt"] = int(now)
        state["conversations"][conv_id] = conv_state
        _save_notify_state(data_path, state)

    elif event_name == "PreInvocation" and invocation_num == 0:
        ft = parsed.get("freshnessTarget") if isinstance(parsed.get("freshnessTarget"), dict) else None
        idle_sec = int((ft.get("idleSeconds") if ft else parsed.get("idleSeconds")) or 0)
        stale_ctx = int((ft.get("contextTokens") if ft else ctx) or 0)
        stale_model = str((ft.get("model") if ft else model_label) or model_label)
        stale_limit = get_model_context_limit(stale_model)
        stale_pct = (stale_ctx / stale_limit * 100.0) if stale_limit > 0 else pct_limit
        last_stale_ts = float(conv_state.get("lastStaleTs") or 0)
        if (
            notify_cache
            and idle_sec >= cache_ttl
            and stale_ctx >= int(warn_tokens * 0.6)
            and (now - last_stale_ts) >= 120
        ):
            chosen = {
                "urgency": "normal",
                "title": "⏱️ キャッシュ期限超過の可能性",
                "body": (
                    f"前回から {fmt_duration(idle_sec)} 経過 · {fmt_k(stale_ctx)} ({stale_pct:.1f}%) · "
                    f"{stale_model} ({session_title})"
                ),
                "kind": "stale_pre",
            }
            conv_state["lastStaleTs"] = now
            conv_state["updatedAt"] = int(now)
            state["conversations"][conv_id] = conv_state
            _save_notify_state(data_path, state)

    if chosen is None:
        return None

    dispatch_notification(
        settings=settings,
        title=chosen["title"],
        body=chosen["body"],
        urgency=chosen["urgency"],
        replace_key=f"ctx:{conv_id}",
        icon_path=icon_path,
        metadata={
            "event": chosen["kind"],
            "conversationId": conv_id,
            "contextTokens": ctx,
            "cacheHitRate": latest.get("cacheHitRate"),
            "model": model_label,
        },
        wait=False,
    )
    return chosen


def maybe_notify_pre_expiry(
    data_dir: str | Path,
    parsed: dict[str, Any],
    settings: dict[str, Any],
    icon_path: str | Path | None = None,
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    """Notify once per model/step when an Orchestrator or single model nears cache TTL expiry."""
    if parsed.get("justCompacted"):
        return []
    if not (settings.get("notifyOnCacheExpiry", True) and settings.get("notifyBeforeExpiry", True)):
        return []
    has_channel = bool(settings.get("desktopNotifyEnabled", True) or settings.get("remoteWebhookUrl"))
    if not has_channel:
        return []

    caches = parsed.get("modelCaches") or []
    if not caches:
        return []

    now = now_ts if now_ts is not None else time.time()
    cache_ttl = int(settings.get("cacheTtlSec", 300))
    min_ctx = int(int(settings.get("warnTokens", 80_000)) * 0.6)
    window_start = max(30, cache_ttl - 60)
    window_end = cache_ttl + 120

    data_path = Path(data_dir)
    state = _load_notify_state(data_path)
    pre_exp = state["preExpiryNotified"]
    conv_id = str(parsed.get("conversationId") or "default")
    session_title = str(parsed.get("title") or "Untitled session")

    sent_events: list[dict[str, Any]] = []
    state_changed = False

    for m in caches:
        if m.get("staleAfterCompact"):
            continue
        if parsed.get("isOrchestrator") and m.get("role") == "executor":
            continue
        ctx = int(m.get("contextTokens") or 0)
        idle_sec = int(m.get("idleSeconds") or 0)
        model_name = str(m.get("model") or "unknown")
        step_idx = m.get("stepIndex", 0)
        dedup_key = f"{conv_id}:{model_name}:{step_idx}"

        if ctx < min_ctx:
            continue
        if not (window_start <= idle_sec < window_end):
            continue
        if dedup_key in pre_exp:
            continue

        title = "⏳ キャッシュ切れ直前"
        body = (
            f"{model_name} · {fmt_k(ctx)} · 経過 {fmt_duration(idle_sec)} / "
            f"TTL {fmt_duration(cache_ttl)} ({session_title})"
        )
        dispatch_notification(
            settings=settings,
            title=title,
            body=body,
            urgency="normal",
            replace_key=f"pre-expiry:{conv_id}:{model_name}",
            icon_path=icon_path,
            metadata={
                "event": "pre_expiry",
                "conversationId": conv_id,
                "model": model_name,
                "contextTokens": ctx,
                "idleSeconds": idle_sec,
            },
            wait=False,
        )
        pre_exp[dedup_key] = int(now)
        state_changed = True
        sent_events.append({"model": model_name, "contextTokens": ctx, "idleSeconds": idle_sec})

    if state_changed:
        _save_notify_state(data_path, state)

    return sent_events


def main() -> None:
    if "--webhook-stdin" in sys.argv:
        try:
            raw = sys.stdin.read()
            job = json.loads(raw) if raw.strip() else {}
            if isinstance(job, dict) and job.get("url"):
                _send_webhook_sync(
                    url=str(job["url"]),
                    title=str(job.get("title") or ""),
                    body=str(job.get("body") or ""),
                    urgency=str(job.get("urgency") or "normal"),
                    token=str(job.get("token") or ""),
                    metadata=job.get("metadata") if isinstance(job.get("metadata"), dict) else None,
                )
        except Exception:
            pass


if __name__ == "__main__":
    main()
