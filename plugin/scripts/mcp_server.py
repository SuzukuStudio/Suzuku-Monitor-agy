#!/usr/bin/env python3
"""MCP App stdio server for Suzuku-Monitor-agy."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from notifier import (  # noqa: E402
    maybe_notify_pre_expiry,
    send_desktop_notification,
    send_remote_webhook,
)
from transcript_parser import (  # noqa: E402
    fmt_duration,
    fmt_k,
    get_brain_dir,
    get_default_data_dir,
    list_sessions,
    load_active_session,
    load_settings,
    parse_transcript,
    save_settings,
)

APP_URI = "ui://suzuku-monitor/app.html"
UI_MIME_TYPE = "text/html;profile=mcp-app"
PROTOCOL_VERSION_DEFAULT = "2025-03-26"


class ContextMonitorServer:
    """Newline-delimited JSON-RPC 2.0 MCP server with UI resources and pre-expiry watcher."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.parse_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}
        self._stop_event = threading.Event()
        if os.environ.get("CONTEXT_MONITOR_DISABLE_WATCHER") != "1":
            t = threading.Thread(target=self._watcher_loop, name="context-pre-expiry-watcher", daemon=True)
            t.start()

    def _watcher_loop(self) -> None:
        """Periodically check if the active session's model caches are nearing TTL expiry."""
        while not self._stop_event.wait(15.0):
            try:
                settings = load_settings(self.data_dir)
                if not settings.get("notifyBeforeExpiry", True):
                    continue
                if not (settings.get("desktopNotifyEnabled", True) or settings.get("remoteWebhookUrl")):
                    continue
                target = self._resolve_target_session()
                if target is None:
                    continue
                fresh = parse_transcript(target["transcriptPath"], now_ts=time.time())
                maybe_notify_pre_expiry(self.data_dir, fresh, settings)
            except Exception:
                pass

    def _load_app_html(self) -> str:
        override = os.environ.get("CONTEXT_MONITOR_APP_HTML")
        html_path = Path(override).expanduser() if override else (_SCRIPT_DIR.parent / "ui" / "app.html")
        if not html_path.is_file():
            raise FileNotFoundError(f"MCP App HTML not found at {html_path}")
        return html_path.read_text(encoding="utf-8")

    def _summarize_session(self, s: dict[str, Any]) -> dict[str, Any]:
        latest = s.get("latest")
        just_compacted = bool(s.get("justCompacted"))
        last_comp = s.get("lastCompaction") or {}
        ctx_tokens = (
            int(last_comp.get("summaryTokens") or 0)
            if just_compacted
            else (latest["contextTokens"] if latest else 0)
        )
        return {
            "conversationId": s["conversationId"],
            "title": s["title"],
            "model": s["model"],
            "contextLimit": s["contextLimit"],
            "contextTokens": ctx_tokens,
            "cacheReadTokens": latest["cacheReadTokens"] if latest else 0,
            "inputTokens": latest["inputTokens"] if latest else 0,
            "cacheHitRate": latest["cacheHitRate"] if latest else 0.0,
            "idleSeconds": s["idleSeconds"],
            "lastActivityAt": s["lastActivityAt"],
            "userTurns": s["userTurns"],
            "modelCalls": s["modelCalls"],
            "isOrchestrator": bool(s.get("isOrchestrator")),
            "justCompacted": just_compacted,
            "isActive": bool(s.get("isActive")),
            "isMostRecent": bool(s.get("isMostRecent")),
            "mtimeMs": s.get("mtimeMs", 0),
        }

    def _resolve_target_session(
        self,
        requested_conv_id: str | None = None,
        now_ts: float | None = None,
    ) -> dict[str, Any] | None:
        now = now_ts if now_ts is not None else time.time()
        active_info = load_active_session(self.data_dir)
        sessions = list_sessions(
            brain_dir=get_brain_dir(),
            active_info=active_info,
            limit=10,
            cache=self.parse_cache,
            now_ts=now,
        )
        return self._pick_selected(sessions, active_info, requested_conv_id)

    @staticmethod
    def _pick_selected(
        sessions: list[dict[str, Any]],
        active_info: dict[str, Any],
        requested_conv_id: str | None = None,
    ) -> dict[str, Any] | None:
        if requested_conv_id:
            for s in sessions:
                if s["conversationId"] == requested_conv_id:
                    return s

        if not sessions:
            return None

        active_conv_id = active_info.get("conversationId") or None
        active_updated = float(active_info.get("updatedAt") or 0)
        newest = sessions[0]
        newest_mtime_sec = float(newest.get("mtimeMs") or 0) / 1000.0

        # Prefer newest file if active-session.json is missing or noticeably stale (>2s behind).
        if not active_conv_id or (newest_mtime_sec > active_updated + 2.0):
            return newest

        for s in sessions:
            if s["conversationId"] == active_conv_id:
                return s
        return newest

    def _build_snapshot(self, requested_conv_id: str | None = None) -> dict[str, Any]:
        now = time.time()
        settings = load_settings(self.data_dir)
        active_info = load_active_session(self.data_dir)
        brain_dir = get_brain_dir()
        sessions = list_sessions(
            brain_dir=brain_dir,
            active_info=active_info,
            limit=30,
            cache=self.parse_cache,
            now_ts=now,
        )

        active_conv_id = active_info.get("conversationId") or None
        active_tp = active_info.get("transcriptPath")
        if active_conv_id and active_tp and not any(s["conversationId"] == active_conv_id for s in sessions):
            tp = Path(active_tp)
            if tp.is_file():
                try:
                    extra = parse_transcript(tp, now_ts=now)
                    extra["isActive"] = True
                    extra["isMostRecent"] = len(sessions) == 0
                    sessions.insert(0, extra)
                except OSError:
                    pass

        picked = self._pick_selected(sessions, active_info, requested_conv_id)
        selected: dict[str, Any] | None = None
        if picked is not None:
            # Always parse the selected session fresh from disk (bypassing parse_cache) for live accuracy.
            try:
                fresh = parse_transcript(picked["transcriptPath"], now_ts=now)
                fresh["isActive"] = bool(
                    (active_conv_id and fresh["conversationId"] == active_conv_id)
                    or picked.get("isMostRecent")
                )
                fresh["isMostRecent"] = bool(picked.get("isMostRecent"))
                selected = fresh
                for idx, s in enumerate(sessions):
                    if s["conversationId"] == fresh["conversationId"]:
                        sessions[idx] = fresh
                        break
            except OSError:
                selected = picked

        if not active_conv_id and sessions:
            active_conv_id = sessions[0]["conversationId"]

        selected_payload: dict[str, Any] | None = None
        if selected is not None:
            selected_payload = {
                k: v for k, v in selected.items() if k not in ("mtimeNs", "lastActivityTs", "fallbackTitle")
            }
            if isinstance(selected_payload.get("modelCaches"), list):
                selected_payload["modelCaches"] = [
                    {k: v for k, v in mc.items() if k != "lastActivityTs"}
                    for mc in selected_payload["modelCaches"]
                ]
            if isinstance(selected_payload.get("freshnessTarget"), dict):
                selected_payload["freshnessTarget"] = {
                    k: v for k, v in selected_payload["freshnessTarget"].items() if k != "lastActivityTs"
                }

        return {
            "generatedAt": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "activeConversationId": active_conv_id,
            "selectedConversationId": selected["conversationId"] if selected else None,
            "sessions": [self._summarize_session(s) for s in sessions],
            "selected": selected_payload,
            "settings": settings,
        }

    def _format_text_summary(self, snapshot: dict[str, Any]) -> str:
        selected = snapshot.get("selected")
        if not selected:
            return "No active session transcript with model token metrics found yet."
        if selected.get("justCompacted") and selected.get("lastCompaction"):
            lc = selected["lastCompaction"]
            orch_tag = " [Orchestrator]" if selected.get("isOrchestrator") else ""
            return (
                f"Session {selected['conversationId']} ({selected['title']}){orch_tag}: "
                f"Compacted ({fmt_k(lc.get('beforeTokens', 0))} -> ~{fmt_k(lc.get('summaryTokens', 0))}), "
                f"awaiting next turn to rebuild prompt cache."
            )
        if not selected.get("latest"):
            return "No active session transcript with model token metrics found yet."
        latest = selected["latest"]
        ctx = latest["contextTokens"]
        limit = selected["contextLimit"]
        pct = (ctx / limit * 100.0) if limit > 0 else 0.0
        cache_pct = round(latest["cacheHitRate"] * 100)
        idle_str = fmt_duration(selected["idleSeconds"])
        orch_tag = " [Orchestrator]" if selected.get("isOrchestrator") else ""
        return (
            f"Session {selected['conversationId']} ({selected['title']}){orch_tag}: "
            f"Context {fmt_k(ctx)} / {fmt_k(limit)} ({pct:.1f}%), "
            f"Cache hit {cache_pct}% ({fmt_k(latest['cacheReadTokens'])} hit, {fmt_k(latest['inputTokens'])} new), "
            f"Idle {idle_str}, Model {selected['model'] or 'unknown'}."
        )

    def handle_request(self, req: dict[str, Any]) -> dict[str, Any] | None:
        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params") if isinstance(req.get("params"), dict) else {}

        if req_id is None or (isinstance(method, str) and method.startswith("notifications/")):
            return None

        try:
            if method == "initialize":
                proto = params.get("protocolVersion") or PROTOCOL_VERSION_DEFAULT
                result = {
                    "protocolVersion": proto,
                    "capabilities": {
                        "tools": {},
                        "resources": {},
                    },
                    "serverInfo": {
                        "name": "suzuku-monitor",
                        "version": "1.2.0",
                    },
                }
                return {"jsonrpc": "2.0", "id": req_id, "result": result}

            if method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}

            if method == "resources/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "resources": [
                            {
                                "uri": APP_URI,
                                "name": "Suzuku Monitor UI",
                                "description": "Interactive context and cache monitor UI",
                                "mimeType": UI_MIME_TYPE,
                                "_meta": {"ui": {"prefersBorder": False}},
                            }
                        ]
                    },
                }

            if method == "resources/read":
                uri = params.get("uri")
                if uri != APP_URI:
                    return self._error(req_id, -32602, f"Unknown resource URI: {uri}")
                html = self._load_app_html()
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "contents": [
                            {
                                "uri": APP_URI,
                                "mimeType": UI_MIME_TYPE,
                                "text": html,
                            }
                        ]
                    },
                }

            if method == "tools/list":
                tools = [
                    {
                        "name": "open_suzuku_monitor",
                        "title": "Suzuku Monitor",
                        "description": "Open the live session context usage and prompt cache monitor panel.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "conversationId": {
                                    "type": "string",
                                    "description": "Optional conversation ID to focus.",
                                }
                            },
                            "additionalProperties": False,
                        },
                        "_meta": {
                            "ui": {"resourceUri": APP_URI},
                            "com.google.antigravity/ui": {
                                "entrypoints": [
                                    {"type": "aux_pane", "title": "Suzuku Monitor"},
                                    {"type": "global", "title": "Suzuku Monitor"},
                                ]
                            },
                        },
                    },
                    {
                        "name": "app_get_context_snapshot",
                        "title": "Get Context Snapshot",
                        "description": "Fetch latest context token metrics and session list for the UI.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "conversationId": {"type": "string"},
                            },
                            "additionalProperties": False,
                        },
                        "_meta": {"ui": {"visibility": ["app"]}},
                    },
                    {
                        "name": "app_update_settings",
                        "title": "Update Suzuku Monitor Settings",
                        "description": "Persist threshold, cache TTL, and notification settings.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "warnTokens": {"type": "integer"},
                                "dangerTokens": {"type": "integer"},
                                "cacheTtlSec": {"type": "integer"},
                                "pollIntervalMs": {"type": "integer"},
                                "desktopNotifyEnabled": {"type": "boolean"},
                                "notifyOnTurnEnd": {"type": "boolean"},
                                "notifyBeforeExpiry": {"type": "boolean"},
                                "remoteWebhookUrl": {"type": "string"},
                                "remoteWebhookToken": {"type": "string"},
                                "chatWarningOnDanger": {"type": "boolean"},
                            },
                            "additionalProperties": False,
                        },
                        "_meta": {"ui": {"visibility": ["app"]}},
                    },
                    {
                        "name": "app_test_notification",
                        "title": "Send Test Notification",
                        "description": "Send a test desktop and/or Remote Control webhook notification.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "desktop": {"type": "boolean"},
                                "webhook": {"type": "boolean"},
                            },
                            "additionalProperties": False,
                        },
                        "_meta": {"ui": {"visibility": ["app"]}},
                    },
                ]
                return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

            if method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                if name in ("open_suzuku_monitor", "app_get_context_snapshot"):
                    conv_id = args.get("conversationId")
                    snapshot = self._build_snapshot(
                        requested_conv_id=conv_id if isinstance(conv_id, str) and conv_id else None
                    )
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": self._format_text_summary(snapshot)}],
                            "structuredContent": snapshot,
                        },
                    }
                if name == "app_update_settings":
                    updated = save_settings(self.data_dir, args)
                    snapshot = self._build_snapshot()
                    snapshot["settings"] = updated
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": "Settings updated."}],
                            "structuredContent": snapshot,
                        },
                    }
                if name == "app_test_notification":
                    settings = load_settings(self.data_dir)
                    test_desktop = (
                        bool(args["desktop"])
                        if "desktop" in args
                        else bool(settings.get("desktopNotifyEnabled", True))
                    )
                    webhook_url = str(settings.get("remoteWebhookUrl") or "").strip()
                    test_webhook = bool(args["webhook"]) if "webhook" in args else bool(webhook_url)

                    title = "🧪 Suzuku Monitor テスト通知"
                    body = "デスクトップ & スマホ Remote Control 連携テスト"
                    desktop_res = {"attempted": False, "sent": False, "detail": "disabled"}
                    webhook_res = {"attempted": False, "sent": False, "detail": "not configured"}

                    if test_desktop:
                        ok, detail = send_desktop_notification(
                            title=title,
                            body=body,
                            urgency="normal",
                            replace_key="suzuku-monitor-test",
                            wait=True,
                        )
                        desktop_res = {"attempted": True, "sent": ok, "detail": detail}

                    if test_webhook:
                        if webhook_url:
                            ok, detail = send_remote_webhook(
                                url=webhook_url,
                                title=title,
                                body=body,
                                urgency="normal",
                                token=str(settings.get("remoteWebhookToken") or ""),
                                metadata={"event": "test"},
                                wait=True,
                            )
                            webhook_res = {"attempted": True, "sent": ok, "detail": detail}
                        else:
                            webhook_res = {"attempted": True, "sent": False, "detail": "webhook URL is empty"}

                    overall_ok = (not test_desktop or desktop_res["sent"]) and (
                        not test_webhook or webhook_res["sent"]
                    )
                    res_payload = {
                        "ok": overall_ok,
                        "desktop": desktop_res,
                        "webhook": webhook_res,
                    }
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Desktop: {desktop_res['detail']} | Webhook: {webhook_res['detail']}",
                                }
                            ],
                            "structuredContent": res_payload,
                        },
                    }
                return self._error(req_id, -32602, f"Unknown tool: {name}")

            return self._error(req_id, -32601, f"Method not found: {method}")
        except Exception as exc:
            return self._error(req_id, -32603, f"Internal error: {exc}")

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def main() -> None:
    parser = argparse.ArgumentParser(description="Suzuku Monitor MCP Server")
    parser.add_argument("--data-dir", default=str(get_default_data_dir()), help="Persistent plugin data directory")
    args, _ = parser.parse_known_args()

    server = ContextMonitorServer(Path(args.data_dir).expanduser())
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                + "\n"
            )
            sys.stdout.flush()
            continue

        if not isinstance(req, dict):
            continue
        resp = server.handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
