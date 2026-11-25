#!/usr/bin/env python3
"""Automated verification suite for suzuku-monitor-agy plugin v1.x.x."""

from __future__ import annotations

import http.server
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "plugin"
SCRIPTS_DIR = PLUGIN_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import context_hook  # noqa: E402
import notifier  # noqa: E402
import transcript_parser  # noqa: E402


def _iso_ago(seconds: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_fixture_transcript(path: Path, entries: list[dict], trailing_corrupt: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(item, ensure_ascii=False) for item in entries]
    if trailing_corrupt:
        lines.append('{"step_index": 99, "broken": ')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _WebhookRecorderHandler(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        type(self).received.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class ContextMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.brain_dir = self.tmp_path / "brain"
        self.data_dir = self.tmp_path / "data"
        self.brain_dir.mkdir()
        self.data_dir.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parse_orchestrator_and_cache_transitions(self) -> None:
        conv_id = "11111111-2222-3333-4444-555555555555"
        tp = self.brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        _write_fixture_transcript(
            tp,
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": _iso_ago(420),
                    "content": "<USER_REQUEST>\nOrchestrator cache test\n</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(400),
                    "model": "gemini-4-neon-high",
                    "input_tokens": 25000,
                    "cache_read_tokens": 0,
                    "output_tokens": 300,
                    "tool_calls": [{"name": "read_file"}],
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(280),
                    "model": "gemini-4-hydrogen-high",
                    "input_tokens": 30000,
                    "cache_read_tokens": 0,
                    "output_tokens": 400,
                    "tool_calls": [{"name": "run_command"}],
                },
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(260),
                    "model": "gemini-4-hydrogen-high",
                    "input_tokens": 4000,
                    "cache_read_tokens": 30000,
                    "output_tokens": 200,
                    "tool_calls": [],
                },
                {
                    "step_index": 4,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(20),
                    "model": "gemini-4-neon-high",
                    "input_tokens": 40000,
                    "cache_read_tokens": 0,
                    "output_tokens": 500,
                    "tool_calls": [],
                },
            ],
            trailing_corrupt=True,
        )

        parsed = transcript_parser.parse_transcript(tp)
        self.assertTrue(parsed["isOrchestrator"])
        self.assertEqual(len(parsed["modelCaches"]), 2)
        self.assertEqual(parsed["modelCaches"][0]["model"], "gemini-4-neon-high")
        self.assertTrue(parsed["modelCaches"][0]["isCurrent"])
        self.assertFalse(parsed["modelCaches"][1]["isCurrent"])

        # Step 2: first switch to hydrogen must NOT be flagged as cacheMiss
        self.assertTrue(parsed["history"][1]["modelSwitch"])
        self.assertFalse(parsed["history"][1]["cacheMiss"])

        # Step 4: returning to stale neon with cache_read == 0 IS cacheMiss and delta is from step 1 (25k -> 40k = +15k)
        self.assertTrue(parsed["history"][3]["modelSwitch"])
        self.assertTrue(parsed["history"][3]["cacheMiss"])
        self.assertEqual(parsed["deltaTokens"], 15000)

    def test_compaction_checkpoint_handling(self) -> None:
        conv_id = "22222222-3333-4444-5555-666666666666"
        tp = self.brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        entries = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": _iso_ago(600),
                "content": "<USER_REQUEST>\nCompaction test\n</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": _iso_ago(500),
                "model": "gemini-4-neon-high",
                "input_tokens": 120000,
                "cache_read_tokens": 0,
                "output_tokens": 500,
                "tool_calls": [],
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "created_at": _iso_ago(400),
                "model": "gemini-4-hydrogen-high",
                "input_tokens": 4000,
                "cache_read_tokens": 259000,
                "output_tokens": 400,
                "tool_calls": [],
            },
            {
                "step_index": 3,
                "source": "SYSTEM",
                "type": "CHECKPOINT",
                "created_at": _iso_ago(120),
                "input_tokens": 302976,
                "cache_read_tokens": 0,
                "output_tokens": 5294,
            },
        ]
        _write_fixture_transcript(tp, entries)

        parsed = transcript_parser.parse_transcript(tp)
        self.assertTrue(parsed["justCompacted"])
        self.assertEqual(parsed["compactionCount"], 1)
        self.assertIsNotNone(parsed["lastCompaction"])
        self.assertEqual(parsed["lastCompaction"]["beforeTokens"], 302976)
        self.assertEqual(parsed["lastCompaction"]["summaryTokens"], 5294)

        cache_models = [m["model"] for m in parsed["modelCaches"]]
        self.assertNotIn("unknown", cache_models)
        for m in parsed["modelCaches"]:
            self.assertTrue(m["staleAfterCompact"])
            self.assertFalse(m["isCurrent"])

        self.assertNotEqual(parsed["history"][-1]["contextTokens"], 302976)
        self.assertEqual(transcript_parser.get_model_context_limit("gemini-4-neon-high"), 512_000)

        settings = transcript_parser.normalize_settings(
            {"desktopNotifyEnabled": True, "notifyBeforeExpiry": True}
        )
        self.assertIsNone(
            notifier.maybe_notify_from_parsed(self.data_dir, parsed, settings, "PostInvocation")
        )
        self.assertEqual(notifier.maybe_notify_pre_expiry(self.data_dir, parsed, settings), [])

        # Append post-compaction calls for Neon and Hydrogen
        entries.extend(
            [
                {
                    "step_index": 4,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(30),
                    "model": "gemini-4-neon-high",
                    "input_tokens": 22562,
                    "cache_read_tokens": 0,
                    "output_tokens": 975,
                    "tool_calls": [],
                },
                {
                    "step_index": 5,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(10),
                    "model": "gemini-4-hydrogen-high",
                    "input_tokens": 25000,
                    "cache_read_tokens": 0,
                    "output_tokens": 300,
                    "tool_calls": [],
                },
            ]
        )
        _write_fixture_transcript(tp, entries)

        parsed_after = transcript_parser.parse_transcript(tp)
        self.assertFalse(parsed_after["justCompacted"])
        self.assertTrue(parsed_after["history"][-2]["compacted"])
        self.assertFalse(parsed_after["history"][-2]["cacheMiss"])
        self.assertTrue(parsed_after["history"][-1]["modelSwitch"])
        self.assertFalse(parsed_after["history"][-1]["cacheMiss"])

        after_models = [m["model"] for m in parsed_after["modelCaches"]]
        self.assertNotIn("unknown", after_models)
        for m in parsed_after["modelCaches"]:
            self.assertFalse(m["staleAfterCompact"])

    def test_orchestrator_executor_turn_handoff_and_planner_freshness(self) -> None:
        conv_id = "33333333-4444-5555-6666-777777777777"
        tp = self.brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        entries = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": _iso_ago(350),
                "content": "<USER_REQUEST>\nTurn 1\n</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": _iso_ago(300),
                "model": "gemini-4-neon-high",
                "input_tokens": 120000,
                "cache_read_tokens": 0,
                "output_tokens": 300,
                "tool_calls": [{"name": "exit_plan_mode"}],
            },
            {
                "step_index": 2,
                "type": "PLANNER_RESPONSE",
                "created_at": _iso_ago(280),
                "model": "gemini-4-hydrogen-high",
                "input_tokens": 130000,
                "cache_read_tokens": 0,
                "output_tokens": 400,
                "tool_calls": [],
            },
            {
                "step_index": 3,
                "type": "USER_INPUT",
                "created_at": _iso_ago(260),
                "content": "<USER_REQUEST>\nTurn 2\n</USER_REQUEST>",
            },
            {
                "step_index": 4,
                "type": "PLANNER_RESPONSE",
                "created_at": _iso_ago(250),
                "model": "gemini-4-neon-high",
                "input_tokens": 30000,
                "cache_read_tokens": 120000,
                "output_tokens": 250,
                "tool_calls": [{"name": "exit_plan_mode"}],
            },
            {
                "step_index": 5,
                "type": "PLANNER_RESPONSE",
                "created_at": _iso_ago(50),
                "model": "gemini-4-hydrogen-high",
                "input_tokens": 148880,
                "cache_read_tokens": 17136,
                "output_tokens": 350,
                "tool_calls": [],
            },
        ]
        _write_fixture_transcript(tp, entries)

        parsed = transcript_parser.parse_transcript(tp)
        handoff_step = parsed["history"][-1]
        self.assertTrue(handoff_step["modelSwitch"])
        self.assertTrue(handoff_step["handoffRebuild"])
        self.assertFalse(handoff_step["cacheMiss"])

        self.assertEqual(len(parsed["modelCaches"]), 2)
        self.assertEqual(parsed["modelCaches"][0]["model"], "gemini-4-neon-high")
        self.assertEqual(parsed["modelCaches"][0]["role"], "planner")
        self.assertEqual(parsed["modelCaches"][1]["model"], "gemini-4-hydrogen-high")
        self.assertEqual(parsed["modelCaches"][1]["role"], "executor")

        self.assertIsNotNone(parsed["freshnessTarget"])
        self.assertEqual(parsed["freshnessTarget"]["model"], "gemini-4-neon-high")
        self.assertEqual(parsed["freshnessTarget"]["role"], "planner")

        # Pre-expiry notification must fire only for Planner (not Executor) even if both are in window
        settings = transcript_parser.normalize_settings(
            {
                "desktopNotifyEnabled": False,
                "notifyBeforeExpiry": True,
                "warnTokens": 80000,
                "cacheTtlSec": 300,
                "remoteWebhookUrl": "http://127.0.0.1:9/unused",
            }
        )
        synthetic = {
            "conversationId": conv_id,
            "title": "Pre-expiry role filter test",
            "isOrchestrator": True,
            "justCompacted": False,
            "modelCaches": [
                {
                    "model": "gemini-4-neon-high",
                    "role": "planner",
                    "contextTokens": 150000,
                    "idleSeconds": 250,
                    "stepIndex": 4,
                    "staleAfterCompact": False,
                },
                {
                    "model": "gemini-4-hydrogen-high",
                    "role": "executor",
                    "contextTokens": 166016,
                    "idleSeconds": 260,
                    "stepIndex": 5,
                    "staleAfterCompact": False,
                },
            ],
        }
        sent = notifier.maybe_notify_pre_expiry(self.data_dir, synthetic, settings)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["model"], "gemini-4-neon-high")

        # Same-turn sudden miss on Executor MUST still be flagged as cacheMiss
        entries.append(
            {
                "step_index": 6,
                "type": "PLANNER_RESPONSE",
                "created_at": _iso_ago(10),
                "model": "gemini-4-hydrogen-high",
                "input_tokens": 150000,
                "cache_read_tokens": 0,
                "output_tokens": 200,
                "tool_calls": [],
            }
        )
        _write_fixture_transcript(tp, entries)
        parsed_same_turn_miss = transcript_parser.parse_transcript(tp)
        miss_step = parsed_same_turn_miss["history"][-1]
        self.assertFalse(miss_step["handoffRebuild"])
        self.assertTrue(miss_step["cacheMiss"])

    def test_parse_real_current_transcript_if_present(self) -> None:
        real_brain = Path.home() / ".gemini" / "antigravity" / "brain"
        if not real_brain.exists():
            return
        files = list(real_brain.glob("*/.system_generated/logs/transcript.jsonl"))
        if not files:
            return
        for f in files:
            parsed = transcript_parser.parse_transcript(f)
            self.assertIn("conversationId", parsed)
            self.assertIn("modelCaches", parsed)
            self.assertNotIn("unknown", [m["model"] for m in parsed["modelCaches"]])

    def test_notifier_formats_and_pre_expiry_dedup(self) -> None:
        # 1. Verify webhook payload builders
        _, ntfy_headers, ntfy_body = notifier.build_webhook_request(
            "https://ntfy.sh/my-secret-topic",
            "🔥 Cache Miss 検知",
            "85.0k tokens",
            urgency="critical",
            token="tok123",
        )
        # Header must be ASCII/latin-1 safe
        ntfy_headers["Title"].encode("latin-1")
        self.assertEqual(ntfy_headers["Click"], "https://antigravity.google.com")
        self.assertEqual(ntfy_headers["Priority"], "urgent")
        self.assertEqual(ntfy_headers["Authorization"], "Bearer tok123")
        self.assertIn("Cache Miss", ntfy_body.decode("utf-8"))

        _, disc_headers, disc_body = notifier.build_webhook_request(
            "https://discord.com/api/webhooks/123/abc",
            "Title",
            "Body",
        )
        self.assertIn("application/json", disc_headers["Content-Type"])
        self.assertIn("**Title**", json.loads(disc_body.decode("utf-8"))["content"])

        _, slack_headers, slack_body = notifier.build_webhook_request(
            "https://hooks.slack.com/services/T/B/X",
            "Title",
            "Body",
        )
        self.assertIn("application/json", slack_headers["Content-Type"])
        self.assertIn("*Title*", json.loads(slack_body.decode("utf-8"))["text"])

        # 2. Verify desktop env fallback
        env = notifier._build_desktop_env()
        self.assertIn("XDG_RUNTIME_DIR", env)

        # 3. Verify pre-expiry detection & deduplication using loopback HTTP server
        _WebhookRecorderHandler.received = []
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _WebhookRecorderHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            settings = transcript_parser.normalize_settings(
                {
                    "desktopNotifyEnabled": False,
                    "notifyBeforeExpiry": True,
                    "warnTokens": 80000,
                    "cacheTtlSec": 300,
                    "remoteWebhookUrl": f"http://127.0.0.1:{port}/hook",
                }
            )
            parsed = {
                "conversationId": "conv-pre-expiry",
                "title": "Pre-expiry test",
                "modelCaches": [
                    {
                        "model": "gemini-4-neon-high",
                        "contextTokens": 60000,
                        "idleSeconds": 250,
                        "stepIndex": 10,
                    }
                ],
            }
            sent1 = notifier.maybe_notify_pre_expiry(self.data_dir, parsed, settings)
            self.assertEqual(len(sent1), 1)
            # Duplicate call for same step must be suppressed
            sent2 = notifier.maybe_notify_pre_expiry(self.data_dir, parsed, settings)
            self.assertEqual(len(sent2), 0)

            # Give detached webhook subprocess up to 2s to hit loopback server
            deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
            while not _WebhookRecorderHandler.received and datetime.now(timezone.utc) < deadline:
                threading.Event().wait(0.05)
            self.assertGreaterEqual(len(_WebhookRecorderHandler.received), 1)
            rec_payload = json.loads(_WebhookRecorderHandler.received[0]["body"])
            self.assertEqual(rec_payload["event"], "pre_expiry")
        finally:
            srv.shutdown()
            srv.server_close()

    def _run_hook(self, payload: dict) -> dict:
        env = dict(os.environ)
        env["PLUGIN_DATA"] = str(self.data_dir)
        env["ANTIGRAVITY_BRAIN_DIR"] = str(self.brain_dir)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "context_hook.py")],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        return json.loads(proc.stdout.strip())

    def test_lifecycle_hook_sync_and_optin_chat_warn(self) -> None:
        conv_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        tp = self.brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        tp_full = self.brain_dir / conv_id / ".system_generated" / "logs" / "transcript_full.jsonl"

        _write_fixture_transcript(
            tp,
            [
                {"step_index": 0, "type": "USER_INPUT", "created_at": _iso_ago(500), "content": "Test hook"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(400),
                    "model": "gemini-4-neon-high",
                    "input_tokens": 5000,
                    "cache_read_tokens": 155000,
                    "output_tokens": 120,
                    "tool_calls": [],
                },
            ],
        )
        tp_full.write_text("", encoding="utf-8")

        transcript_parser.save_settings(self.data_dir, {"desktopNotifyEnabled": False})
        res1 = self._run_hook(
            {
                "hookEventName": "PreInvocation",
                "invocationNum": 0,
                "conversationId": conv_id,
                "transcriptPath": str(tp_full),
                "modelName": "gemini-4-neon-high",
            }
        )
        self.assertEqual(res1, {})
        active_data = json.loads((self.data_dir / "active-session.json").read_text(encoding="utf-8"))
        self.assertEqual(active_data["conversationId"], conv_id)
        self.assertEqual(active_data["transcriptPath"], str(tp))

        # When chatWarningOnDanger is enabled, PreInvocation invocationNum=0 emits plain systemMessage
        transcript_parser.save_settings(
            self.data_dir,
            {"desktopNotifyEnabled": False, "chatWarningOnDanger": True},
        )
        res2 = self._run_hook(
            {
                "hookEventName": "PreInvocation",
                "invocationNum": 0,
                "conversationId": conv_id,
                "transcriptPath": str(tp),
                "modelName": "gemini-4-neon-high",
            }
        )
        steps = res2.get("injectSteps", [])
        self.assertEqual(len(steps), 1)
        self.assertIn("[Suzuku Monitor]", steps[0]["systemMessage"]["systemMessage"])
        self.assertNotIn("metadata", steps[0]["systemMessage"])

    def test_mcp_server_stdio_protocol_and_test_notification(self) -> None:
        conv_id = "99999999-8888-7777-6666-555555555555"
        tp = self.brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        _write_fixture_transcript(
            tp,
            [
                {"step_index": 0, "type": "USER_INPUT", "created_at": _iso_ago(90), "content": "Inspect MCP server"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(20),
                    "model": "gemini-4-neon-high",
                    "input_tokens": 6000,
                    "cache_read_tokens": 34000,
                    "output_tokens": 450,
                    "tool_calls": [],
                },
            ],
        )
        transcript_parser.save_active_session(
            self.data_dir,
            {"conversationId": conv_id, "transcriptPath": str(tp), "updatedAt": int(datetime.now(timezone.utc).timestamp())},
        )

        _WebhookRecorderHandler.received = []
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _WebhookRecorderHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        env = dict(os.environ)
        env["ANTIGRAVITY_BRAIN_DIR"] = str(self.brain_dir)
        env["CONTEXT_MONITOR_DISABLE_WATCHER"] = "1"
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS_DIR / "mcp_server.py"), "--data-dir", str(self.data_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert proc.stdin is not None and proc.stdout is not None

        def rpc(msg: dict) -> dict:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        try:
            init_resp = rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
                }
            )
            self.assertEqual(init_resp["result"]["serverInfo"]["name"], "suzuku-monitor")
            self.assertEqual(init_resp["result"]["serverInfo"]["version"], "1.2.0")

            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()

            tools_list = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools_by_name = {tool["name"]: tool for tool in tools_list["result"]["tools"]}
            self.assertIn("open_suzuku_monitor", tools_by_name)
            entrypoints = tools_by_name["open_suzuku_monitor"]["_meta"]["com.google.antigravity/ui"]["entrypoints"]
            self.assertTrue(all(ep.get("title") == "Suzuku Monitor" for ep in entrypoints))
            self.assertIn("app_test_notification", tools_by_name)
            self.assertEqual(tools_by_name["app_test_notification"]["_meta"]["ui"]["visibility"], ["app"])

            call_upd = rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "app_update_settings",
                        "arguments": {
                            "desktopNotifyEnabled": False,
                            "remoteWebhookUrl": f"http://127.0.0.1:{port}/test",
                            "pollIntervalMs": 2000,
                        },
                    },
                }
            )
            self.assertEqual(call_upd["result"]["structuredContent"]["settings"]["pollIntervalMs"], 2000)

            call_test = rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "app_test_notification", "arguments": {"desktop": False, "webhook": True}},
                }
            )
            sc_test = call_test["result"]["structuredContent"]
            self.assertTrue(sc_test["ok"])
            self.assertTrue(sc_test["webhook"]["sent"])
            self.assertGreaterEqual(len(_WebhookRecorderHandler.received), 1)
        finally:
            if proc.stdin:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            proc.terminate()
            proc.wait(timeout=5)
            srv.shutdown()
            srv.server_close()

    def test_app_display_title_resolution_and_live_refresh(self) -> None:
        db_path = self.tmp_path / "conversation_summaries.db"
        ann_dir = self.tmp_path / "annotations"
        ann_dir.mkdir(parents=True, exist_ok=True)

        def make_transcript(cid: str, first_msg: str, mtime_offset: int) -> Path:
            tp = self.brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
            _write_fixture_transcript(
                tp,
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": _iso_ago(mtime_offset + 10), "content": first_msg},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": _iso_ago(mtime_offset),
                        "model": "gemini-4-neon-high",
                        "input_tokens": 5000,
                        "cache_read_tokens": 10000,
                        "output_tokens": 200,
                        "tool_calls": [],
                    },
                ],
            )
            return tp

        cid_db = "aaaa1111-2222-3333-4444-555555555555"
        cid_ann = "bbbb1111-2222-3333-4444-555555555555"
        cid_prev = "cccc1111-2222-3333-4444-555555555555"
        cid_fb = "dddd1111-2222-3333-4444-555555555555"

        tp_db = make_transcript(cid_db, "Raw user prompt in transcript", 40)
        tp_ann = make_transcript(cid_ann, "Annotation fallback prompt", 30)
        tp_prev = make_transcript(cid_prev, "Preview fallback prompt", 20)
        tp_fb = make_transcript(cid_fb, "Transcript only fallback title", 10)

        long_preview = "P" * 80
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', preview TEXT NOT NULL DEFAULT '')"
            )
            con.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
                (cid_db, "App DB Display Title", "Ignored preview"),
            )
            con.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
                (cid_ann, "", "Should be shadowed by annotation"),
            )
            con.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
                (cid_prev, "", long_preview),
            )
            con.commit()
        finally:
            con.close()

        (ann_dir / f"{cid_ann}.pbtxt").write_text(
            'title:"日本語 \\"引用\\" タイトル" last_user_view_time:{seconds:100}',
            encoding="utf-8",
        )

        # 1-4: Verify priority order via parse_transcript and list_sessions
        self.assertEqual(transcript_parser.parse_transcript(tp_db)["title"], "App DB Display Title")
        self.assertEqual(transcript_parser.parse_transcript(tp_ann)["title"], '日本語 "引用" タイトル')
        self.assertEqual(transcript_parser.parse_transcript(tp_prev)["title"], ("P" * 61) + "...")
        self.assertEqual(transcript_parser.parse_transcript(tp_fb)["title"], "Transcript only fallback title")

        cache: dict = {}
        sessions1 = transcript_parser.list_sessions(brain_dir=self.brain_dir, cache=cache)
        by_id1 = {s["conversationId"]: s for s in sessions1}
        self.assertEqual(by_id1[cid_db]["title"], "App DB Display Title")
        self.assertEqual(by_id1[cid_ann]["title"], '日本語 "引用" タイトル')
        self.assertEqual(by_id1[cid_prev]["title"], ("P" * 61) + "...")
        self.assertEqual(by_id1[cid_fb]["title"], "Transcript only fallback title")

        # 5: Update DB title WITHOUT touching transcript.jsonl -> cache hit still reflects updated title
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?",
                ("Manually Renamed Title", cid_db),
            )
            con.commit()
        finally:
            con.close()

        sessions2 = transcript_parser.list_sessions(brain_dir=self.brain_dir, cache=cache)
        by_id2 = {s["conversationId"]: s for s in sessions2}
        self.assertEqual(by_id2[cid_db]["title"], "Manually Renamed Title")

        # 6: Corrupt DB falls back gracefully to annotation / transcript without raising
        db_path.write_text("not a sqlite database", encoding="utf-8")
        sessions3 = transcript_parser.list_sessions(brain_dir=self.brain_dir, cache=cache)
        by_id3 = {s["conversationId"]: s for s in sessions3}
        self.assertEqual(by_id3[cid_db]["title"], "Raw user prompt in transcript")
        self.assertEqual(by_id3[cid_ann]["title"], '日本語 "引用" タイトル')
        self.assertEqual(by_id3[cid_fb]["title"], "Transcript only fallback title")

    def test_cross_platform_desktop_notification_and_detached_spawn(self) -> None:
        # 1. macOS (darwin) passes title and body via argv rather than string interpolation
        special_title = 'Alert "Quote" & \\ $HOME'
        special_body = "Line 1\nLine '2'"
        with (
            mock.patch.object(notifier.sys, "platform", "darwin"),
            mock.patch.object(notifier.shutil, "which", return_value="/usr/bin/osascript"),
            mock.patch.object(notifier.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            ok, detail = notifier.send_desktop_notification(special_title, special_body, wait=True)
            self.assertTrue(ok)
            self.assertEqual(detail, "ok")
            called_cmd = mock_run.call_args[0][0]
            self.assertEqual(called_cmd[0], "/usr/bin/osascript")
            self.assertIn("on run argv", called_cmd)
            self.assertEqual(called_cmd[-3:], ["--", special_title, special_body])

        # 2. _build_desktop_env handles missing os.getuid (Windows-like) without raising
        with mock.patch.object(notifier.os, "getuid", None, create=True):
            env = notifier._build_desktop_env()
            self.assertIsInstance(env, dict)

        # 3. _popen_detached uses creationflags on Windows and omits start_new_session
        with (
            mock.patch.object(notifier.os, "name", "nt"),
            mock.patch.object(notifier.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
            mock.patch.object(notifier.subprocess, "DETACHED_PROCESS", 0x00000008, create=True),
            mock.patch.object(notifier.subprocess, "Popen") as mock_popen,
        ):
            notifier._popen_detached(["python", "script.py"])
            kwargs = mock_popen.call_args.kwargs
            self.assertNotIn("start_new_session", kwargs)
            self.assertEqual(kwargs["creationflags"], 0x08000008)

    def test_swarm_coordinator_parsing_and_worker_aggregation(self) -> None:
        coord_cid = "c0000000-0000-0000-0000-000000000001"
        w_impl_cid = "w1111111-0000-0000-0000-000000000002"
        w_adv_cid = "w2222222-0000-0000-0000-000000000003"
        w_dead_cid = "w3333333-0000-0000-0000-000000000004"
        w_stop_cid = "w4444444-0000-0000-0000-000000000006"
        plain_cid = "p4444444-0000-0000-0000-000000000005"

        coord_tp = self.brain_dir / coord_cid / ".system_generated" / "logs" / "transcript.jsonl"
        _write_fixture_transcript(
            coord_tp,
            [
                {"step_index": 0, "type": "USER_INPUT", "created_at": _iso_ago(200), "content": "/swarm task"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(180),
                    "model": "gemini-4-neon-high",
                    "input_tokens": 20000,
                    "cache_read_tokens": 0,
                    "output_tokens": 500,
                    "tool_calls": [{"name": "swarm"}],
                },
            ],
        )

        # Working worker (last model invocation called run_command)
        _write_fixture_transcript(
            self.brain_dir / w_impl_cid / ".system_generated" / "logs" / "transcript.jsonl",
            [
                {"step_index": 0, "type": "SYSTEM_MESSAGE", "created_at": _iso_ago(170), "content": "brief"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(160),
                    "model": "gemini-4-hydrogen-xhigh",
                    "input_tokens": 12000,
                    "cache_read_tokens": 0,
                    "output_tokens": 300,
                    "tool_calls": [{"name": "read_file"}],
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(30),
                    "model": "gemini-4-hydrogen-xhigh",
                    "input_tokens": 3000,
                    "cache_read_tokens": 12000,
                    "output_tokens": 200,
                    "tool_calls": [{"name": "run_command"}],
                },
            ],
        )

        # Waiting worker (parked on swarm_wait)
        _write_fixture_transcript(
            self.brain_dir / w_adv_cid / ".system_generated" / "logs" / "transcript.jsonl",
            [
                {
                    "step_index": 0,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(100),
                    "model": "gemini-4-carbon-high",
                    "input_tokens": 15000,
                    "cache_read_tokens": 0,
                    "output_tokens": 400,
                    "tool_calls": [{"name": "swarm_wait"}],
                },
                {"step_index": 1, "type": "GENERIC", "created_at": _iso_ago(99), "content": "Waiting"},
            ],
        )

        # Killed worker
        _write_fixture_transcript(
            self.brain_dir / w_dead_cid / ".system_generated" / "logs" / "transcript.jsonl",
            [
                {
                    "step_index": 0,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(150),
                    "model": "gemini-3.8-flash-high",
                    "input_tokens": 5000,
                    "cache_read_tokens": 0,
                    "output_tokens": 100,
                    "tool_calls": [{"name": "read_file"}],
                }
            ],
        )

        # Stopped worker
        _write_fixture_transcript(
            self.brain_dir / w_stop_cid / ".system_generated" / "logs" / "transcript.jsonl",
            [
                {
                    "step_index": 0,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(140),
                    "model": "gemini-4-hydrogen-high",
                    "input_tokens": 4000,
                    "cache_read_tokens": 1000,
                    "output_tokens": 50,
                    "tool_calls": [{"name": "swarm_wait"}],
                }
            ],
        )

        swarm_dir = self.brain_dir / coord_cid / ".system_generated" / "swarm"
        swarm_dir.mkdir(parents=True, exist_ok=True)
        (swarm_dir / "swarm.json").write_text(
            json.dumps({"version": 1, "backend": "local", "swarm_id": coord_cid}),
            encoding="utf-8",
        )
        (swarm_dir / "members.json").write_text(
            json.dumps(
                {
                    "title": "",
                    "members": {
                        "coordinator": {
                            "conversation_id": coord_cid,
                            "role": "coordinator",
                            "model": "gemini-4-neon-high",
                            "killed": False,
                        },
                        "implementer": {
                            "conversation_id": w_impl_cid,
                            "role": "worker",
                            "desired_model": "gemini-4-hydrogen-xhigh",
                            "applied_model": "gemini-4-hydrogen-xhigh",
                            "wake_mode": "working",
                            "killed": False,
                        },
                        "advisor": {
                            "conversation_id": w_adv_cid,
                            "role": "worker",
                            "model": "gemini-4-carbon-high",
                            "wake_mode": "mentions",
                            "killed": False,
                        },
                        "old-helper": {
                            "conversation_id": w_dead_cid,
                            "role": "worker",
                            "model": "gemini-3.8-flash-high",
                            "killed": True,
                        },
                        "paused-helper": {
                            "conversation_id": w_stop_cid,
                            "role": "worker",
                            "model": "gemini-4-hydrogen-high",
                            "desired_state": "stopped",
                            "killed": False,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        (swarm_dir / "plan.json").write_text(
            json.dumps({"goal": {"title": "Minimal Swarm Prototype", "line": "Test plan fallback"}}),
            encoding="utf-8",
        )
        (swarm_dir / "board.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"seq": 1, "agent": "coordinator", "text": "@implementer go"}),
                    json.dumps({"seq": 2, "agent": "implementer", "text": "@advisor ready"}),
                    '{"seq": 3, "broken": ',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        parsed = transcript_parser.parse_transcript(coord_tp)
        self.assertTrue(parsed["isSwarm"])
        swarm = parsed["swarm"]
        self.assertIsNotNone(swarm)
        self.assertEqual(swarm["title"], "Minimal Swarm Prototype")
        self.assertEqual(swarm["workerCount"], 4)
        self.assertEqual(swarm["liveWorkerCount"], 2)
        self.assertEqual(swarm["retiredWorkerCount"], 2)
        self.assertEqual(swarm["workingCount"], 1)
        self.assertEqual(swarm["boardMessages"], 2)

        # Coordinator tokens (20k) must NOT be double-counted into worker totals,
        # while both live and retired workers remain included in cumulative totals.
        self.assertEqual(
            swarm["totals"],
            {
                "calls": 5,
                "uncachedInput": 39000,
                "cacheRead": 13000,
                "output": 1050,
            },
        )
        by_name = {w["name"]: w for w in swarm["workers"]}
        self.assertNotIn("coordinator", by_name)
        self.assertEqual(by_name["implementer"]["status"], "working")
        self.assertEqual(by_name["implementer"]["contextTokens"], 15000)
        self.assertEqual(by_name["implementer"]["calls"], 2)
        self.assertEqual(by_name["advisor"]["status"], "waiting")
        self.assertEqual(by_name["old-helper"]["status"], "killed")
        self.assertEqual(by_name["paused-helper"]["status"], "stopped")

        # Non-swarm transcript reports isSwarm=False and swarm=None
        plain_tp = self.brain_dir / plain_cid / ".system_generated" / "logs" / "transcript.jsonl"
        _write_fixture_transcript(
            plain_tp,
            [{"step_index": 0, "type": "USER_INPUT", "created_at": _iso_ago(10), "content": "Normal"}],
        )
        plain_parsed = transcript_parser.parse_transcript(plain_tp)
        self.assertFalse(plain_parsed["isSwarm"])
        self.assertIsNone(plain_parsed["swarm"])

    def test_swarm_list_sessions_filtering_active_remap_and_live_refresh(self) -> None:
        coord_cid = "c1111111-0000-0000-0000-000000000001"
        worker_cid = "w2222222-0000-0000-0000-000000000002"
        other_cid = "o3333333-0000-0000-0000-000000000003"

        coord_tp = self.brain_dir / coord_cid / ".system_generated" / "logs" / "transcript.jsonl"
        worker_tp = self.brain_dir / worker_cid / ".system_generated" / "logs" / "transcript.jsonl"
        other_tp = self.brain_dir / other_cid / ".system_generated" / "logs" / "transcript.jsonl"

        _write_fixture_transcript(
            coord_tp,
            [
                {"step_index": 0, "type": "USER_INPUT", "created_at": _iso_ago(300), "content": "/swarm run"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(290),
                    "model": "gemini-4-neon-high",
                    "input_tokens": 18000,
                    "cache_read_tokens": 0,
                    "output_tokens": 300,
                    "tool_calls": [{"name": "swarm"}],
                },
            ],
        )
        _write_fixture_transcript(
            worker_tp,
            [
                {
                    "step_index": 0,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(120),
                    "model": "gemini-4-hydrogen-xhigh",
                    "input_tokens": 10000,
                    "cache_read_tokens": 0,
                    "output_tokens": 200,
                    "tool_calls": [{"name": "read_file"}],
                }
            ],
        )
        _write_fixture_transcript(
            other_tp,
            [
                {"step_index": 0, "type": "USER_INPUT", "created_at": _iso_ago(200), "content": "Other session"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(190),
                    "model": "gemini-4-neon-high",
                    "input_tokens": 9000,
                    "cache_read_tokens": 0,
                    "output_tokens": 100,
                    "tool_calls": [],
                },
            ],
        )

        swarm_dir = self.brain_dir / coord_cid / ".system_generated" / "swarm"
        swarm_dir.mkdir(parents=True, exist_ok=True)
        (swarm_dir / "members.json").write_text(
            json.dumps(
                {
                    "title": "Demo Swarm",
                    "members": {
                        "coordinator": {"conversation_id": coord_cid, "role": "coordinator"},
                        "implementer": {
                            "conversation_id": worker_cid,
                            "role": "worker",
                            "model": "gemini-4-hydrogen-xhigh",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        (swarm_dir / "board.jsonl").write_text(
            json.dumps({"seq": 1, "agent": "coordinator", "text": "@implementer start"}) + "\n",
            encoding="utf-8",
        )

        # Preserve older mtime on coordinator transcript to verify swarm/worker mtime keeps coordinator fresh
        old_ns = int((datetime.now(timezone.utc) - timedelta(seconds=250)).timestamp() * 1e9)
        os.utime(coord_tp, ns=(old_ns, old_ns))
        mid_ns = int((datetime.now(timezone.utc) - timedelta(seconds=150)).timestamp() * 1e9)
        os.utime(other_tp, ns=(mid_ns, mid_ns))

        cache: dict = {}
        sessions1 = transcript_parser.list_sessions(brain_dir=self.brain_dir, cache=cache)
        cids1 = [s["conversationId"] for s in sessions1]
        self.assertEqual(cids1, [coord_cid, other_cid])
        self.assertNotIn(worker_cid, cids1)
        self.assertTrue(sessions1[0]["isSwarm"])
        self.assertEqual(sessions1[0]["swarm"]["totals"]["calls"], 1)

        # Update worker transcript WITHOUT touching coordinator transcript -> cache invalidates and refreshes
        _write_fixture_transcript(
            worker_tp,
            [
                {
                    "step_index": 0,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(120),
                    "model": "gemini-4-hydrogen-xhigh",
                    "input_tokens": 10000,
                    "cache_read_tokens": 0,
                    "output_tokens": 200,
                    "tool_calls": [{"name": "read_file"}],
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(5),
                    "model": "gemini-4-hydrogen-xhigh",
                    "input_tokens": 2000,
                    "cache_read_tokens": 10000,
                    "output_tokens": 150,
                    "tool_calls": [],
                },
            ],
        )
        sessions2 = transcript_parser.list_sessions(brain_dir=self.brain_dir, cache=cache)
        self.assertEqual(sessions2[0]["swarm"]["totals"]["calls"], 2)
        self.assertEqual(sessions2[0]["swarm"]["workers"][0]["status"], "waiting")

        # MCP snapshot remaps worker active-session.json back to coordinator and omits worker from sessions
        transcript_parser.save_active_session(
            self.data_dir,
            {
                "conversationId": worker_cid,
                "transcriptPath": str(worker_tp),
                "updatedAt": int(datetime.now(timezone.utc).timestamp()),
            },
        )
        env = dict(os.environ)
        env["ANTIGRAVITY_BRAIN_DIR"] = str(self.brain_dir)
        env["CONTEXT_MONITOR_DISABLE_WATCHER"] = "1"
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS_DIR / "mcp_server.py"), "--data-dir", str(self.data_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "app_get_context_snapshot", "arguments": {}},
                    }
                )
                + "\n"
            )
            proc.stdin.flush()
            snap_resp = json.loads(proc.stdout.readline())
        finally:
            if proc.stdin:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            proc.terminate()
            proc.wait(timeout=5)

        res = snap_resp["result"]
        sc = res["structuredContent"]
        self.assertEqual(sc["activeConversationId"], coord_cid)
        self.assertEqual(sc["selectedConversationId"], coord_cid)
        self.assertEqual([s["conversationId"] for s in sc["sessions"]], [coord_cid, other_cid])
        self.assertTrue(sc["sessions"][0]["isSwarm"])
        self.assertEqual(sc["sessions"][0]["swarmWorkers"], 1)
        self.assertEqual(sc["sessions"][0]["swarmLiveWorkers"], 1)
        self.assertIn("[Swarm]", res["content"][0]["text"])
        self.assertEqual(sc["selected"]["swarm"]["totals"]["calls"], 2)

    def test_swarm_all_retired_counts_and_ui_bundle(self) -> None:
        coord_cid = "c5555555-0000-0000-0000-000000000001"
        w_killed_cid = "w5555555-0000-0000-0000-000000000002"
        w_stopped_cid = "w5555555-0000-0000-0000-000000000003"

        coord_tp = self.brain_dir / coord_cid / ".system_generated" / "logs" / "transcript.jsonl"
        _write_fixture_transcript(
            coord_tp,
            [
                {"step_index": 0, "type": "USER_INPUT", "created_at": _iso_ago(120), "content": "/swarm retired"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(110),
                    "model": "gemini-4-neon-high",
                    "input_tokens": 10000,
                    "cache_read_tokens": 0,
                    "output_tokens": 200,
                    "tool_calls": [{"name": "swarm"}],
                },
            ],
        )
        _write_fixture_transcript(
            self.brain_dir / w_killed_cid / ".system_generated" / "logs" / "transcript.jsonl",
            [
                {
                    "step_index": 0,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(90),
                    "model": "gemini-4-hydrogen-high",
                    "input_tokens": 7000,
                    "cache_read_tokens": 3000,
                    "output_tokens": 150,
                    "tool_calls": [{"name": "read_file"}],
                }
            ],
        )
        _write_fixture_transcript(
            self.brain_dir / w_stopped_cid / ".system_generated" / "logs" / "transcript.jsonl",
            [
                {
                    "step_index": 0,
                    "type": "PLANNER_RESPONSE",
                    "created_at": _iso_ago(80),
                    "model": "gemini-4-carbon-high",
                    "input_tokens": 8000,
                    "cache_read_tokens": 2000,
                    "output_tokens": 250,
                    "tool_calls": [],
                }
            ],
        )

        swarm_dir = self.brain_dir / coord_cid / ".system_generated" / "swarm"
        swarm_dir.mkdir(parents=True, exist_ok=True)
        (swarm_dir / "members.json").write_text(
            json.dumps(
                {
                    "title": "All Retired Swarm",
                    "members": {
                        "coordinator": {"conversation_id": coord_cid, "role": "coordinator"},
                        "done-impl": {
                            "conversation_id": w_killed_cid,
                            "role": "worker",
                            "model": "gemini-4-hydrogen-high",
                            "killed": True,
                        },
                        "paused-adv": {
                            "conversation_id": w_stopped_cid,
                            "role": "worker",
                            "model": "gemini-4-carbon-high",
                            "phase": "stopped",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        parsed = transcript_parser.parse_transcript(coord_tp)
        swarm = parsed["swarm"]
        self.assertIsNotNone(swarm)
        self.assertEqual(swarm["workerCount"], 2)
        self.assertEqual(swarm["liveWorkerCount"], 0)
        self.assertEqual(swarm["retiredWorkerCount"], 2)
        self.assertEqual(swarm["workingCount"], 0)
        self.assertEqual(
            swarm["totals"],
            {
                "calls": 2,
                "uncachedInput": 15000,
                "cacheRead": 5000,
                "output": 400,
            },
        )

        app_html = (PLUGIN_DIR / "ui" / "app.html").read_text(encoding="utf-8")
        decoded_html = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda m: chr(int(m.group(1), 16)),
            app_html,
        )
        self.assertIn("data-swarm-retired-toggle", decoded_html)
        self.assertIn("aria-expanded", decoded_html)
        self.assertIn("終了済み (", decoded_html)
        self.assertIn("▼ 表示", decoded_html)
        self.assertIn("▲ 閉じる", decoded_html)
        self.assertIn("稼働中のワーカーはありません", decoded_html)
        self.assertIn("ワーカー未追加", decoded_html)
        self.assertIn("swarm-retired-toggle", decoded_html)
        self.assertIn("swarm-retired-section", decoded_html)

    def test_independent_cache_and_context_notification_gates(self) -> None:
        base_settings = {
            "desktopNotifyEnabled": False,
            "remoteWebhookUrl": "http://127.0.0.1:9/unused",
            "warnTokens": 80000,
            "dangerTokens": 150000,
            "cacheTtlSec": 300,
            "notifyBeforeExpiry": True,
            "notifyOnTurnEnd": True,
            "chatWarningOnDanger": True,
        }

        def make_parsed(
            *,
            cid: str,
            ctx: int,
            cache_miss: bool,
            idle_sec: int,
            step_idx: int,
            is_swarm: bool,
            has_tool_calls: bool = False,
        ) -> dict:
            return {
                "conversationId": cid,
                "title": "Gate Test",
                "model": "gemini-4-neon-high",
                "contextLimit": 512000,
                "idleSeconds": idle_sec,
                "justCompacted": False,
                "isSwarm": is_swarm,
                "latest": {
                    "stepIndex": step_idx,
                    "model": "gemini-4-neon-high",
                    "contextTokens": ctx,
                    "cacheHitRate": 0.0 if cache_miss else 0.9,
                    "cacheMiss": cache_miss,
                    "hasToolCalls": has_tool_calls,
                },
                "modelCaches": [
                    {
                        "model": "gemini-4-neon-high",
                        "role": "primary",
                        "contextTokens": ctx,
                        "idleSeconds": idle_sec,
                        "stepIndex": step_idx,
                        "staleAfterCompact": False,
                    }
                ],
            }

        with mock.patch.object(notifier, "dispatch_notification"):
            # 1 & 3. notifyOnCacheExpiry=False, notifyOnContextGrowth=True (identical for isSwarm=False and True)
            s_ctx_only = transcript_parser.normalize_settings(
                {**base_settings, "notifyOnCacheExpiry": False, "notifyOnContextGrowth": True}
            )
            for is_swarm in (False, True):
                sub_dir = self.data_dir / f"ctx_only_{is_swarm}"
                sub_dir.mkdir(parents=True, exist_ok=True)
                cid = f"conv-ctx-only-{is_swarm}"

                # cache_miss + below warn -> suppressed; falls through to turn_end
                p_miss_low = make_parsed(
                    cid=cid, ctx=50000, cache_miss=True, idle_sec=350, step_idx=1, is_swarm=is_swarm
                )
                res_low = notifier.maybe_notify_from_parsed(
                    sub_dir, p_miss_low, s_ctx_only, "PostInvocation", now_ts=1000
                )
                self.assertIsNotNone(res_low)
                self.assertEqual(res_low["kind"], "turn_end")

                # stale_pre and pre_expiry must be suppressed even when notifyBeforeExpiry=True
                self.assertIsNone(
                    notifier.maybe_notify_from_parsed(
                        sub_dir,
                        make_parsed(cid=cid, ctx=90000, cache_miss=False, idle_sec=350, step_idx=1, is_swarm=is_swarm),
                        s_ctx_only,
                        "PreInvocation",
                        invocation_num=0,
                        now_ts=1010,
                    )
                )
                self.assertEqual(
                    notifier.maybe_notify_pre_expiry(
                        sub_dir,
                        make_parsed(cid=cid, ctx=90000, cache_miss=False, idle_sec=260, step_idx=1, is_swarm=is_swarm),
                        s_ctx_only,
                        now_ts=1010,
                    ),
                    [],
                )

                # cache_miss + warn -> falls through and fires warn
                res_warn = notifier.maybe_notify_from_parsed(
                    sub_dir,
                    make_parsed(cid=cid, ctx=90000, cache_miss=True, idle_sec=10, step_idx=2, is_swarm=is_swarm),
                    s_ctx_only,
                    "PostInvocation",
                    now_ts=1020,
                )
                self.assertIsNotNone(res_warn)
                self.assertEqual(res_warn["kind"], "warn")

                # cache_miss + danger -> falls through and fires danger
                res_danger = notifier.maybe_notify_from_parsed(
                    sub_dir,
                    make_parsed(cid=cid, ctx=160000, cache_miss=True, idle_sec=10, step_idx=3, is_swarm=is_swarm),
                    s_ctx_only,
                    "PostInvocation",
                    now_ts=1030,
                )
                self.assertIsNotNone(res_danger)
                self.assertEqual(res_danger["kind"], "danger")

                # _maybe_chat_warning: stale/miss below danger suppressed; >= danger emits 肥大化
                self.assertEqual(
                    context_hook._maybe_chat_warning(sub_dir, p_miss_low, s_ctx_only, "PreInvocation", 0),
                    {},
                )
                cw_danger = context_hook._maybe_chat_warning(
                    sub_dir,
                    make_parsed(cid=cid, ctx=160000, cache_miss=True, idle_sec=10, step_idx=3, is_swarm=is_swarm),
                    s_ctx_only,
                    "PreInvocation",
                    0,
                )
                msg = cw_danger["injectSteps"][0]["systemMessage"]["systemMessage"]
                self.assertIn("肥大化", msg)
                self.assertNotIn("Cache Miss", msg)

            # 2 & 3. notifyOnCacheExpiry=True, notifyOnContextGrowth=False (identical for isSwarm=False and True)
            s_cache_only = transcript_parser.normalize_settings(
                {**base_settings, "notifyOnCacheExpiry": True, "notifyOnContextGrowth": False}
            )
            for is_swarm in (False, True):
                sub_dir = self.data_dir / f"cache_only_{is_swarm}"
                sub_dir.mkdir(parents=True, exist_ok=True)
                cid = f"conv-cache-only-{is_swarm}"

                # Seed notify-state with sentinel values to ensure warnNotified/lastDangerTs/lastDangerCtx are untouched
                notifier._save_notify_state(
                    sub_dir,
                    {
                        "conversations": {
                            cid: {
                                "lastNotifiedStep": 0,
                                "warnNotified": True,
                                "lastDangerTs": 123.0,
                                "lastDangerCtx": 155000,
                            }
                        },
                        "preExpiryNotified": {},
                    },
                )

                # High context (>= danger) without cacheMiss suppresses danger/warn, falls through to turn_end
                res_high = notifier.maybe_notify_from_parsed(
                    sub_dir,
                    make_parsed(cid=cid, ctx=180000, cache_miss=False, idle_sec=10, step_idx=1, is_swarm=is_swarm),
                    s_cache_only,
                    "PostInvocation",
                    now_ts=2000,
                )
                self.assertIsNotNone(res_high)
                self.assertEqual(res_high["kind"], "turn_end")

                # Low context (< 0.8 * warnTokens) must NOT reset warnNotified while notifyOnContextGrowth=False
                notifier.maybe_notify_from_parsed(
                    sub_dir,
                    make_parsed(
                        cid=cid,
                        ctx=20000,
                        cache_miss=False,
                        idle_sec=10,
                        step_idx=2,
                        is_swarm=is_swarm,
                        has_tool_calls=True,
                    ),
                    s_cache_only,
                    "PostInvocation",
                    now_ts=2010,
                )
                saved_conv = notifier._load_notify_state(sub_dir)["conversations"][cid]
                self.assertTrue(saved_conv["warnNotified"])
                self.assertEqual(saved_conv["lastDangerTs"], 123.0)
                self.assertEqual(saved_conv["lastDangerCtx"], 155000)

                # cache_miss, stale_pre, and pre_expiry all still fire
                res_miss = notifier.maybe_notify_from_parsed(
                    sub_dir,
                    make_parsed(cid=cid, ctx=180000, cache_miss=True, idle_sec=10, step_idx=3, is_swarm=is_swarm),
                    s_cache_only,
                    "PostInvocation",
                    now_ts=2020,
                )
                self.assertIsNotNone(res_miss)
                self.assertEqual(res_miss["kind"], "cache_miss")

                res_stale = notifier.maybe_notify_from_parsed(
                    sub_dir,
                    make_parsed(cid=cid, ctx=90000, cache_miss=False, idle_sec=350, step_idx=3, is_swarm=is_swarm),
                    s_cache_only,
                    "PreInvocation",
                    invocation_num=0,
                    now_ts=2030,
                )
                self.assertIsNotNone(res_stale)
                self.assertEqual(res_stale["kind"], "stale_pre")

                res_pre = notifier.maybe_notify_pre_expiry(
                    sub_dir,
                    make_parsed(cid=cid, ctx=90000, cache_miss=False, idle_sec=260, step_idx=3, is_swarm=is_swarm),
                    s_cache_only,
                    now_ts=2040,
                )
                self.assertEqual(len(res_pre), 1)

                # _maybe_chat_warning: >= danger without miss/stale is suppressed; cache_miss still fires
                self.assertEqual(
                    context_hook._maybe_chat_warning(
                        sub_dir,
                        make_parsed(cid=cid, ctx=180000, cache_miss=False, idle_sec=10, step_idx=4, is_swarm=is_swarm),
                        s_cache_only,
                        "PreInvocation",
                        0,
                    ),
                    {},
                )
                cw_miss = context_hook._maybe_chat_warning(
                    sub_dir,
                    make_parsed(cid=cid, ctx=180000, cache_miss=True, idle_sec=10, step_idx=5, is_swarm=is_swarm),
                    s_cache_only,
                    "PreInvocation",
                    0,
                )
                self.assertIn("Cache Miss", cw_miss["injectSteps"][0]["systemMessage"]["systemMessage"])

    def test_app_update_settings_round_trips_notification_flags(self) -> None:
        env = dict(os.environ)
        env["ANTIGRAVITY_BRAIN_DIR"] = str(self.brain_dir)
        env["CONTEXT_MONITOR_DISABLE_WATCHER"] = "1"
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS_DIR / "mcp_server.py"), "--data-dir", str(self.data_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert proc.stdin is not None and proc.stdout is not None

        def rpc(msg: dict) -> dict:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        try:
            tools_list = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            upd_schema = next(t for t in tools_list["result"]["tools"] if t["name"] == "app_update_settings")[
                "inputSchema"
            ]["properties"]
            self.assertEqual(upd_schema["notifyOnCacheExpiry"]["type"], "boolean")
            self.assertEqual(upd_schema["notifyOnContextGrowth"]["type"], "boolean")

            # Defaults are True
            snap0 = rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "app_get_context_snapshot", "arguments": {}},
                }
            )["result"]["structuredContent"]
            self.assertTrue(snap0["settings"]["notifyOnCacheExpiry"])
            self.assertTrue(snap0["settings"]["notifyOnContextGrowth"])

            # Toggle both False and verify persistence across snapshot calls
            upd1 = rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "app_update_settings",
                        "arguments": {"notifyOnCacheExpiry": False, "notifyOnContextGrowth": False},
                    },
                }
            )["result"]["structuredContent"]
            self.assertFalse(upd1["settings"]["notifyOnCacheExpiry"])
            self.assertFalse(upd1["settings"]["notifyOnContextGrowth"])

            snap1 = rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "app_get_context_snapshot", "arguments": {}},
                }
            )["result"]["structuredContent"]
            self.assertFalse(snap1["settings"]["notifyOnCacheExpiry"])
            self.assertFalse(snap1["settings"]["notifyOnContextGrowth"])
            self.assertFalse(transcript_parser.load_settings(self.data_dir)["notifyOnCacheExpiry"])
            self.assertFalse(transcript_parser.load_settings(self.data_dir)["notifyOnContextGrowth"])

            # Toggle back to True / False mixed
            upd2 = rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "app_update_settings",
                        "arguments": {"notifyOnCacheExpiry": True, "notifyOnContextGrowth": False},
                    },
                }
            )["result"]["structuredContent"]
            self.assertTrue(upd2["settings"]["notifyOnCacheExpiry"])
            self.assertFalse(upd2["settings"]["notifyOnContextGrowth"])
        finally:
            if proc.stdin:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
