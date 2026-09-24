#!/usr/bin/env python3
"""Repair wedged Codex Desktop history projections.

Desktop shortcut: 修复 Codex 对话投影.lnk
  -> codex_unwedge_popup.vbs
  -> start_codex_desktop.ps1 (optional local+server account pick)
  -> this file (python), then optional remote pass over SSH Host.

Account switching is the launcher's job. The desktop shortcut calls
codex_local_csw.py on this PC and `~/.local/bin/csw` on
remote server so each machine swaps only its own ~/.codex/auth.json.
The launcher also refreshes each machine's current-account snapshot from
that machine's live auth.json when the refresh token changed.
config.toml is left alone, and tokens are never copied over SSH. This file
never touches auth.json.

Authoritative history is ~/.codex/sessions/**/rollout-*.jsonl (and archived_sessions).
The UI reads the rebuildable SQLite projection ~/.codex/thread_history_1.sqlite
(thread_history_projection_state, thread_items, thread_turns). Do not rewrite
JSONL except ordinal_backfill below. Deleting projection rows is forbidden:
Desktop thread/read does not rematerialize from JSONL (#40112).

Run only while Codex Desktop / app-server is fully closed. Always backup sqlite
(and any JSONL about to be rewritten) before writing.

Known repair shapes (add a new kind here when a new freeze appears):

1. dup_skip — token_count / thread_settings_applied reuse the cursor ordinal.
   Advance the byte cursor past that metadata line; keep next_ordinal.
2. ordinal_advance / skip_token_count — cursor consumed a token_count line
   without incrementing next_ordinal (#40342 / #38792). Advance ordinal by one
   or skip that metadata line. Do not delete rows.
3. catch_up — cursor is consistent but still behind EOF. Opening the thread
   does not project the JSONL suffix, so replay item_completed / task_started /
   task_complete into sqlite and move the cursor to EOF.
4. ordinal_backfill — a trailing suffix of valid JSONL records has no ordinal.
   Desktop resume then dies with "final paginated rollout record is missing an
   ordinal". Backup the JSONL, write sequential ordinals onto that suffix only,
   then catch_up the same range. Do not touch earlier ordinaled lines.

If a future freeze is not one of these, classify() should return a new note
instead of guessing. Inspect the record at next_rollout_byte_offset, compare
its ordinal to next_rollout_ordinal, and add a repair that keeps existing
thread_items.

Examples:

    python3 codex_unwedge_projection.py --dry-run
    python3 codex_unwedge_projection.py --ssh-host <remote-host> --dry-run
    python codex_unwedge_projection.py
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UUID_RE = __import__("re").compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    __import__("re").IGNORECASE,
)
ALLOWED_DUP_KINDS = frozenset({"token_count", "thread_settings_applied"})
MAX_SKIP_LINES = 2
DB_NAME = "thread_history_1.sqlite"


@dataclass
class CursorState:
    thread_id: str
    offset: int
    next_ordinal: int


@dataclass
class Repair:
    kind: str
    thread_id: str
    jsonl: Path
    old_offset: int
    old_ordinal: int
    new_offset: int | None = None
    new_ordinal: int | None = None
    skipped: list[tuple[int, str]] = field(default_factory=list)
    detail: str = ""
    items_added: int = 0
    turns_upserted: int = 0
    lines_rewritten: int = 0


def _print(text: str) -> None:
    stream = sys.stdout
    encoding = stream.encoding or "utf-8"
    stream.write(text.encode(encoding, errors="replace").decode(encoding, errors="replace") + "\n")


def payload_kind(rec: dict[str, Any]) -> str:
    payload = rec.get("payload") or {}
    if isinstance(payload, dict):
        msg = payload.get("msg")
        if isinstance(msg, dict) and msg.get("type"):
            return str(msg.get("type"))
        if payload.get("type"):
            return str(payload.get("type"))
    return str(rec.get("type") or "")


def event_payload(rec: dict[str, Any]) -> dict[str, Any]:
    payload = rec.get("payload") or {}
    if isinstance(payload, dict):
        msg = payload.get("msg")
        if isinstance(msg, dict) and msg.get("type"):
            return msg
        return payload
    return {}


def snake_to_camel(value: str | None) -> str | None:
    if not value:
        return value
    parts = str(value).split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:] if part)


def timestamp_ms(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def duration_to_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        secs = value.get("secs")
        nanos = value.get("nanos") or 0
        if isinstance(secs, (int, float)):
            return int(secs * 1000 + int(nanos) / 1_000_000)
        millis = value.get("millis") or value.get("ms")
        if isinstance(millis, (int, float)):
            return int(millis)
    if isinstance(value, str):
        text = value.strip().lower()
        if text.endswith("ms"):
            try:
                return int(float(text[:-2]))
            except ValueError:
                return None
        if text.endswith("s") and not text.endswith("ms"):
            try:
                return int(float(text[:-1]) * 1000)
            except ValueError:
                return None
    return None


def native_path_uri(uri: str | None) -> str:
    if not uri:
        return ""
    path = uri
    if path.startswith("file://"):
        path = path[7:]
        if path.startswith("/") and len(path) >= 3 and path[2] == ":":
            path = path[1:]
    if len(path) >= 2 and path[1] == ":":
        path = path.replace("/", "\\")
    return path


def host_path(path: str | None) -> str | None:
    if path is None:
        return None
    if len(path) >= 2 and path[1] == ":":
        return path.replace("/", "\\")
    return path


def join_command(argv: Any) -> str:
    if isinstance(argv, str):
        return argv
    if not isinstance(argv, list):
        return str(argv or "")
    parts = [str(item) for item in argv]
    try:
        return shlex.join(parts)
    except Exception:
        return " ".join(parts)


def convert_command_actions(parsed_cmd: Any, cwd_uri: str | None) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if not isinstance(parsed_cmd, list):
        return actions
    for entry in parsed_cmd:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        command = entry.get("command") or entry.get("cmd") or ""
        if kind == "read":
            actions.append(
                {
                    "type": "read",
                    "command": command,
                    "name": entry.get("name") or "",
                    "path": host_path(entry.get("path")) or native_path_uri(cwd_uri),
                }
            )
        elif kind in {"list_files", "listFiles"}:
            actions.append({"type": "listFiles", "command": command, "path": entry.get("path")})
        elif kind == "search":
            actions.append(
                {
                    "type": "search",
                    "command": command,
                    "query": entry.get("query"),
                    "path": entry.get("path"),
                }
            )
        else:
            actions.append({"type": "unknown", "command": command})
    return actions


def convert_file_changes(changes: Any) -> list[dict[str, Any]]:
    if isinstance(changes, list):
        return [item for item in changes if isinstance(item, dict)]
    if not isinstance(changes, dict):
        return []
    out: list[dict[str, Any]] = []
    for path, change in changes.items():
        if not isinstance(change, dict):
            continue
        kind = change.get("type") or change.get("kind")
        if isinstance(kind, dict):
            out.append({"path": str(path), "kind": kind, "diff": change.get("diff") or ""})
            continue
        if kind == "add":
            mapped_kind: dict[str, Any] = {"type": "add"}
            diff = change.get("content") or change.get("diff") or ""
        elif kind == "delete":
            mapped_kind = {"type": "delete"}
            diff = change.get("content") or change.get("diff") or ""
        else:
            mapped_kind = {"type": "update", "move_path": change.get("move_path")}
            diff = change.get("unified_diff") or change.get("diff") or ""
            if change.get("move_path"):
                diff = f"{diff}\n\nMoved to: {change['move_path']}"
        out.append({"path": str(path), "kind": mapped_kind, "diff": diff})
    out.sort(key=lambda item: str(item.get("path") or ""))
    return out


def convert_memory_citation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    entries = value.get("entries") or []
    thread_ids = value.get("thread_ids") or value.get("rollout_ids") or []
    return {"entries": entries, "threadIds": thread_ids}


def convert_content_items(items: Any) -> list[dict[str, Any]] | None:
    if items is None:
        return None
    if not isinstance(items, list):
        return None
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mapped = dict(item)
        kind = mapped.get("type")
        if isinstance(kind, str) and kind[:1].isupper():
            mapped["type"] = kind[:1].lower() + kind[1:]
        out.append(mapped)
    return out


def convert_turn_item(item: dict[str, Any]) -> dict[str, Any] | None:
    kind = item.get("type")
    item_id = item.get("id")
    if not isinstance(kind, str) or not isinstance(item_id, str) or not item_id:
        return None
    if kind == "UserMessage":
        return {
            "type": "userMessage",
            "id": item_id,
            "clientId": item.get("client_id"),
            "content": item.get("content") or [],
        }
    if kind == "Reasoning":
        return {
            "type": "reasoning",
            "id": item_id,
            "summary": item.get("summary_text") or item.get("summary") or [],
            "content": item.get("raw_content") or item.get("content") or [],
        }
    if kind == "AgentMessage":
        texts: list[str] = []
        for entry in item.get("content") or []:
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                texts.append(entry["text"])
            elif isinstance(entry, str):
                texts.append(entry)
        text = "".join(texts)
        if not text and isinstance(item.get("text"), str):
            text = item["text"]
        return {
            "type": "agentMessage",
            "id": item_id,
            "text": text,
            "phase": item.get("phase"),
            "memoryCitation": convert_memory_citation(item.get("memory_citation")),
        }
    if kind == "Plan":
        return {"type": "plan", "id": item_id, "text": item.get("text") or ""}
    if kind == "CommandExecution":
        aggregated = item.get("aggregated_output")
        if aggregated == "":
            aggregated = None
        return {
            "type": "commandExecution",
            "id": item_id,
            "command": join_command(item.get("command")),
            "cwd": native_path_uri(item.get("cwd")),
            "processId": item.get("process_id"),
            "source": snake_to_camel(item.get("source")) or "agent",
            "status": snake_to_camel(item.get("status")) or "inProgress",
            "commandActions": convert_command_actions(item.get("parsed_cmd"), item.get("cwd")),
            "aggregatedOutput": aggregated,
            "exitCode": item.get("exit_code"),
            "durationMs": duration_to_ms(item.get("duration")),
        }
    if kind == "FileChange":
        status = item.get("status")
        return {
            "type": "fileChange",
            "id": item_id,
            "changes": convert_file_changes(item.get("changes")),
            "status": snake_to_camel(status) or "inProgress",
        }
    if kind == "DynamicToolCall":
        return {
            "type": "dynamicToolCall",
            "id": item_id,
            "namespace": item.get("namespace"),
            "tool": item.get("tool"),
            "arguments": item.get("arguments"),
            "status": snake_to_camel(item.get("status")) or "inProgress",
            "contentItems": convert_content_items(item.get("content_items")),
            "success": item.get("success"),
            "durationMs": duration_to_ms(item.get("duration")),
        }
    if kind == "McpToolCall":
        app_context = None
        if item.get("connector_id"):
            app_context = {
                "connectorId": item.get("connector_id"),
                "linkId": item.get("link_id"),
                "resourceUri": item.get("mcp_app_resource_uri"),
                "appName": item.get("app_name"),
                "actionName": item.get("action_name"),
            }
        return {
            "type": "mcpToolCall",
            "id": item_id,
            "server": item.get("server"),
            "tool": item.get("tool"),
            "status": snake_to_camel(item.get("status")) or "inProgress",
            "arguments": item.get("arguments"),
            "appContext": app_context,
            "pluginId": item.get("plugin_id"),
            "result": item.get("result"),
            "error": item.get("error"),
            "durationMs": duration_to_ms(item.get("duration")),
        }
    if kind == "CollabAgentToolCall":
        states = item.get("agents_states") or {}
        mapped_states = {}
        if isinstance(states, dict):
            for key, value in states.items():
                mapped_states[str(key)] = snake_to_camel(value) if isinstance(value, str) else value
        return {
            "type": "collabAgentToolCall",
            "id": item_id,
            "tool": snake_to_camel(item.get("tool")),
            "status": snake_to_camel(item.get("status")) or "inProgress",
            "senderThreadId": str(item.get("sender_thread_id") or ""),
            "receiverThreadIds": [str(x) for x in (item.get("receiver_thread_ids") or [])],
            "prompt": item.get("prompt"),
            "model": item.get("model"),
            "reasoningEffort": item.get("reasoning_effort"),
            "agentsStates": mapped_states,
        }
    if kind == "SubAgentActivity":
        return {
            "type": "subAgentActivity",
            "id": item_id,
            "kind": snake_to_camel(item.get("kind")),
            "agentThreadId": str(item.get("agent_thread_id") or ""),
            "agentPath": str(item.get("agent_path") or ""),
        }
    if kind == "WebSearch":
        action = item.get("action")
        if isinstance(action, dict) and isinstance(action.get("type"), str):
            action = dict(action)
            action["type"] = snake_to_camel(action.get("type")) or action.get("type")
        return {
            "type": "webSearch",
            "id": item_id,
            "query": item.get("query") or "",
            "action": action,
            "results": item.get("results"),
        }
    if kind == "Extension":
        nested = item.get("item") if isinstance(item.get("item"), dict) else None
        nested_type = None
        if isinstance(nested, dict):
            nested_type = nested.get("type")
        nested_type = nested_type or item.get("kind") or item.get("extension_type")
        if nested_type == "WebSearch" or item.get("query") is not None:
            payload = dict(nested or item)
            payload["type"] = "WebSearch"
            payload["id"] = item_id
            return convert_turn_item(payload)
        if nested_type == "ImageGeneration":
            payload = dict(nested or item)
            payload["type"] = "ImageGeneration"
            payload["id"] = item_id
            return convert_turn_item(payload)
        if nested_type == "Sleep":
            return {"type": "sleep", "id": item_id, **{k: v for k, v in (nested or item).items() if k not in {"type", "id"}}}
        if isinstance(nested, dict):
            return convert_turn_item({**nested, "id": item_id})
    if kind == "ImageView":
        return {"type": "imageView", "id": item_id, "path": native_path_uri(item.get("path"))}
    if kind == "ContextCompaction":
        return {"type": "contextCompaction", "id": item_id}
    if kind == "HookPrompt":
        return {"type": "hookPrompt", "id": item_id, "fragments": item.get("fragments") or []}
    if kind in {"EnteredReviewMode", "ExitedReviewMode"}:
        mapped_type = "enteredReviewMode" if kind == "EnteredReviewMode" else "exitedReviewMode"
        return {"type": mapped_type, "id": item_id, "review": item.get("review") or item.get("user_facing_hint") or ""}
    if kind[:1].isupper():
        mapped = {"type": kind[:1].lower() + kind[1:], "id": item_id}
        for key, value in item.items():
            if key in {"type", "id"}:
                continue
            mapped[snake_to_camel(key) or key] = value
        return mapped
    return None


def synthetic_item_id(kind: str, ordinal: Any) -> str:
    return f"bf-{kind}-{ordinal}"


def project_event(
    rec: dict[str, Any],
    last_turn_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    payload = event_payload(rec)
    kind = str(payload.get("type") or rec.get("type") or "")
    turns: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    turn_id_out = last_turn_id
    ordinal = rec.get("ordinal")
    if kind in {"task_started", "turn_started"}:
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            turns.append(
                {
                    "turn_id": turn_id,
                    "status": "inProgress",
                    "error_json": None,
                    "started_at": payload.get("started_at"),
                    "completed_at": None,
                    "duration_ms": None,
                    "terminal": False,
                }
            )
    elif kind in {"task_complete", "turn_complete"}:
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            failed = payload.get("error") is not None
            error_json = None
            if failed:
                try:
                    error_json = json.dumps(payload.get("error"), ensure_ascii=False)
                except (TypeError, ValueError):
                    error_json = json.dumps({"message": str(payload.get("error"))})
            turns.append(
                {
                    "turn_id": turn_id,
                    "status": "failed" if failed else "completed",
                    "error_json": error_json,
                    "started_at": payload.get("started_at"),
                    "completed_at": payload.get("completed_at"),
                    "duration_ms": payload.get("duration_ms"),
                    "terminal": True,
                }
            )
    elif kind in {"task_aborted", "turn_aborted"}:
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            turns.append(
                {
                    "turn_id": turn_id,
                    "status": "interrupted",
                    "error_json": None,
                    "started_at": payload.get("started_at"),
                    "completed_at": payload.get("completed_at"),
                    "duration_ms": payload.get("duration_ms"),
                    "terminal": True,
                }
            )
    elif kind == "item_completed":
        raw_item = payload.get("item")
        turn_id = payload.get("turn_id")
        if isinstance(raw_item, dict) and isinstance(turn_id, str) and turn_id:
            converted = convert_turn_item(raw_item)
            if converted is not None:
                created_at_ms = payload.get("started_at_ms")
                if not isinstance(created_at_ms, int):
                    created_at_ms = timestamp_ms(rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None)
                items.append(
                    {
                        "turn_id": turn_id,
                        "item": converted,
                        "created_at_ms": created_at_ms,
                    }
                )
            turn_id_out = turn_id
    elif kind in {"user_message", "UserMessage"}:
        turn_id = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else last_turn_id
        text = payload.get("message")
        if not isinstance(text, str):
            text = payload.get("text") if isinstance(payload.get("text"), str) else ""
        if isinstance(turn_id, str) and turn_id and text:
            item_id = payload.get("id")
            if not isinstance(item_id, str) or not item_id:
                item_id = synthetic_item_id("user", ordinal)
            created_at_ms = timestamp_ms(rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None)
            items.append(
                {
                    "turn_id": turn_id,
                    "item": {
                        "type": "userMessage",
                        "id": item_id,
                        "content": [{"type": "text", "text": text}],
                    },
                    "created_at_ms": created_at_ms,
                }
            )
            turn_id_out = turn_id
    elif kind in {"agent_message", "AgentMessage"}:
        turn_id = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else last_turn_id
        text = payload.get("message")
        if not isinstance(text, str):
            text = payload.get("text") if isinstance(payload.get("text"), str) else ""
        if isinstance(turn_id, str) and turn_id and text:
            item_id = payload.get("id")
            if not isinstance(item_id, str) or not item_id:
                item_id = synthetic_item_id("agent", ordinal)
            created_at_ms = timestamp_ms(rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None)
            items.append(
                {
                    "turn_id": turn_id,
                    "item": {
                        "type": "agentMessage",
                        "id": item_id,
                        "text": text,
                    },
                    "created_at_ms": created_at_ms,
                }
            )
            turn_id_out = turn_id
    if turns:
        turn_id_out = turns[-1]["turn_id"]
    return turns, items, turn_id_out


def replay_suffix(
    path: Path, start_offset: int, expected_ordinal: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int, int]:
    """Replay JSONL from a consistent cursor. Returns turn ops, item ops, next offset, next ordinal, line count."""
    size = path.stat().st_size
    turns: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    next_offset = start_offset
    next_ordinal = expected_ordinal
    lines = 0
    last_turn_id: str | None = None
    with path.open("rb") as handle:
        handle.seek(start_offset)
        while True:
            line_start = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            if not raw.endswith(b"\n") and line_start + len(raw) != size:
                break
            if not raw.strip():
                next_offset = line_start + len(raw)
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                next_offset = line_start + len(raw)
                continue
            if not isinstance(rec, dict):
                next_offset = line_start + len(raw)
                continue
            ordinal = rec.get("ordinal")
            if not isinstance(ordinal, int):
                break
            if ordinal < next_ordinal:
                next_offset = line_start + len(raw)
                continue
            line_turns, line_items, last_turn_id = project_event(rec, last_turn_id)
            for turn in line_turns:
                turn["rollout_ordinal"] = ordinal
                turn["rollout_byte_offset"] = line_start
                turn["rollout_end_byte_offset"] = line_start + len(raw)
                turns.append(turn)
            for item in line_items:
                item["rollout_ordinal"] = ordinal
                if not isinstance(item.get("created_at_ms"), int):
                    item["created_at_ms"] = timestamp_ms(
                        rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None
                    )
                if not isinstance(item.get("created_at_ms"), int):
                    item["created_at_ms"] = 0
                items.append(item)
            next_ordinal = ordinal + 1
            next_offset = line_start + len(raw)
            lines += 1
    return turns, items, next_offset, next_ordinal, lines


def apply_turn_op(cur: sqlite3.Cursor, thread_id: str, turn: dict[str, Any]) -> None:
    terminal = bool(turn.get("terminal"))
    terminal_ordinal = turn["rollout_ordinal"] if terminal else None
    terminal_offset = turn["rollout_end_byte_offset"] if terminal else None
    cur.execute(
        """
        INSERT INTO thread_turns (
            thread_id, turn_id, rollout_ordinal, rollout_byte_offset,
            rollout_end_ordinal, rollout_end_byte_offset, status, error_json,
            started_at, completed_at, duration_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id, turn_id) DO UPDATE SET
            rollout_end_ordinal = excluded.rollout_end_ordinal,
            rollout_end_byte_offset = excluded.rollout_end_byte_offset,
            status = excluded.status,
            error_json = excluded.error_json,
            started_at = excluded.started_at,
            completed_at = excluded.completed_at,
            duration_ms = excluded.duration_ms
        WHERE thread_turns.rollout_end_ordinal IS NULL
          AND thread_turns.status = 'inProgress'
        """,
        (
            thread_id,
            turn["turn_id"],
            turn["rollout_ordinal"],
            turn["rollout_byte_offset"],
            terminal_ordinal,
            terminal_offset,
            turn["status"],
            turn.get("error_json"),
            turn.get("started_at"),
            turn.get("completed_at"),
            turn.get("duration_ms"),
        ),
    )
    cur.execute(
        """
        UPDATE thread_turns
        SET
            first_user_item_id = COALESCE(
                first_user_item_id,
                (
                    SELECT item_id FROM thread_items
                    WHERE thread_id = ? AND turn_id = ?
                      AND json_extract(item_json, '$.type') = 'userMessage'
                    ORDER BY rollout_ordinal LIMIT 1
                )
            ),
            final_agent_item_id = COALESCE(
                (
                    SELECT item_id FROM thread_items
                    WHERE thread_id = ? AND turn_id = ?
                      AND json_extract(item_json, '$.type') = 'agentMessage'
                      AND json_extract(item_json, '$.phase') = 'final_answer'
                    ORDER BY rollout_ordinal DESC LIMIT 1
                ),
                CASE
                    WHEN status IN ('completed', 'interrupted', 'failed') THEN (
                        SELECT item_id FROM thread_items
                        WHERE thread_id = ? AND turn_id = ?
                          AND json_extract(item_json, '$.type') = 'agentMessage'
                          AND json_extract(item_json, '$.phase') IS NULL
                        ORDER BY rollout_ordinal DESC LIMIT 1
                    )
                END,
                final_agent_item_id
            )
        WHERE thread_id = ? AND turn_id = ?
          AND (rollout_end_ordinal = ? OR status = 'inProgress')
        """,
        (
            thread_id,
            turn["turn_id"],
            thread_id,
            turn["turn_id"],
            thread_id,
            turn["turn_id"],
            thread_id,
            turn["turn_id"],
            turn["rollout_ordinal"],
        ),
    )


def apply_item_op(cur: sqlite3.Cursor, thread_id: str, item: dict[str, Any]) -> None:
    created_at_ms = item.get("created_at_ms")
    if not isinstance(created_at_ms, int):
        raise RuntimeError(f"{thread_id}: missing item created_at_ms")
    snapshot = item["item"]
    item_id = snapshot["id"]
    item_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    cur.execute(
        """
        INSERT INTO thread_items (
            thread_id, turn_id, item_id, rollout_ordinal, updated_at_ordinal,
            created_at_ms, item_type, item_json
        ) VALUES (?, ?, ?, ?, ?, ?, json_extract(?, '$.type'), ?)
        ON CONFLICT(thread_id, turn_id, item_id) DO UPDATE SET
            updated_at_ordinal = excluded.updated_at_ordinal,
            item_type = excluded.item_type,
            item_json = excluded.item_json
        """,
        (
            thread_id,
            item["turn_id"],
            item_id,
            item["rollout_ordinal"],
            item["rollout_ordinal"],
            created_at_ms,
            item_json,
            item_json,
        ),
    )
    item_type = snapshot.get("type")
    phase = snapshot.get("phase")
    if item_type == "userMessage":
        cur.execute(
            """
            UPDATE thread_turns
            SET first_user_item_id = COALESCE(first_user_item_id, ?)
            WHERE thread_id = ? AND turn_id = ?
              AND rollout_end_ordinal IS NULL AND status = 'inProgress'
            """,
            (item_id, thread_id, item["turn_id"]),
        )
    elif item_type == "agentMessage" and phase == "final_answer":
        cur.execute(
            """
            UPDATE thread_turns
            SET final_agent_item_id = ?
            WHERE thread_id = ? AND turn_id = ?
              AND rollout_end_ordinal IS NULL AND status = 'inProgress'
            """,
            (item_id, thread_id, item["turn_id"]),
        )


def apply_catch_up(
    cur: sqlite3.Cursor,
    item: Repair,
    replay_from_item: bool = False,
) -> tuple[int, int, int, int]:
    row = cur.execute(
        """
        SELECT next_rollout_byte_offset, next_rollout_ordinal
        FROM thread_history_projection_state
        WHERE thread_id = ?
        """,
        (item.thread_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"{item.thread_id}: missing projection state")
    sqlite_offset, sqlite_ord = int(row[0]), int(row[1])
    start_offset = item.old_offset if replay_from_item else sqlite_offset
    expected_ordinal = item.old_ordinal if replay_from_item else sqlite_ord
    if replay_from_item and sqlite_ord != item.old_ordinal:
        raise RuntimeError(
            f"{item.thread_id}: ordinal_backfill expected ordinal {item.old_ordinal}, sqlite has {sqlite_ord}"
        )
    turns, items, next_offset, next_ordinal, _lines = replay_suffix(
        item.jsonl, start_offset, expected_ordinal
    )
    for turn in turns:
        apply_turn_op(cur, item.thread_id, turn)
    for projected in items:
        apply_item_op(cur, item.thread_id, projected)
    cur.execute(
        """
        UPDATE thread_history_projection_state
        SET next_rollout_byte_offset = ?,
            next_rollout_ordinal = ?
        WHERE thread_id = ?
          AND next_rollout_byte_offset = ?
          AND next_rollout_ordinal = ?
        """,
        (next_offset, next_ordinal, item.thread_id, sqlite_offset, sqlite_ord),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"{item.thread_id}: catch_up affected {cur.rowcount} cursor rows")
    return next_offset, next_ordinal, len(items), len(turns)


def sqlite_unlocked(path: Path) -> bool:
    try:
        con = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=0.2)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
        finally:
            con.close()
        return True
    except sqlite3.Error:
        return False


def index_rollouts(home: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = defaultdict(list)
    for root_name in ("sessions", "archived_sessions"):
        root = home / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            for match in UUID_RE.findall(path.name):
                index[match.lower()].append(path)
    return index


def offset_is_record_boundary(path: Path, offset: int) -> bool:
    size = path.stat().st_size
    if offset < 0 or offset > size:
        return False
    if offset == size:
        return True
    with path.open("rb") as handle:
        if offset > 0:
            handle.seek(offset - 1)
            if handle.read(1) != b"\n":
                return False
        handle.seek(offset)
        raw = handle.readline()
    if not raw:
        return False
    if not raw.endswith(b"\n") and offset + len(raw) != size:
        return False
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(rec, dict)


def pick_jsonl(candidates: list[Path], offset: int) -> Path | None:
    fits = [
        path
        for path in candidates
        if path.exists() and offset_is_record_boundary(path, offset)
    ]
    if not fits:
        return None
    return max(fits, key=lambda path: (path.stat().st_mtime, path.stat().st_size))


def read_line_at(path: Path, offset: int) -> tuple[bytes, dict[str, Any]] | None:
    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.readline()
    if not raw:
        return None
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    return raw, rec


def inspect_jsonl(path: Path) -> tuple[int | None, bool]:
    size = path.stat().st_size
    if size == 0:
        return None, False
    bufsize = min(size, 65536)
    with path.open("rb") as handle:
        while True:
            handle.seek(max(0, size - bufsize))
            chunk = handle.read(bufsize)
            lines = [line.strip() for line in chunk.split(b"\n") if line.strip()]
            for raw in reversed(lines):
                try:
                    rec = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(rec, dict):
                    ordinal = rec.get("ordinal")
                    if isinstance(ordinal, int):
                        return ordinal, False
            if bufsize >= size:
                break
            bufsize = min(size, bufsize * 4)
    return None, False


def previous_line(path: Path, offset: int) -> tuple[bytes, dict[str, Any]] | None:
    if offset <= 0:
        return None
    with path.open("rb") as handle:
        handle.seek(offset - 1)
        if handle.read(1) != b"\n":
            return None
        pos = offset - 2
        while pos > 0:
            handle.seek(pos)
            if handle.read(1) == b"\n":
                start = pos + 1
                break
            pos -= 1
        else:
            start = 0
        handle.seek(start)
        raw = handle.read(offset - start)
    if not raw:
        return None
    try:
        rec = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    return raw, rec


def plan_dup_skip(path: Path, offset: int, next_ordinal: int) -> Repair | None:
    skipped: list[tuple[int, str]] = []
    pos = offset
    for _ in range(MAX_SKIP_LINES):
        item = read_line_at(path, pos)
        if item is None:
            return None
        raw, rec = item
        ordinal = rec.get("ordinal")
        kind = payload_kind(rec)
        if ordinal == next_ordinal:
            break
        if ordinal == next_ordinal - 1 and kind in ALLOWED_DUP_KINDS:
            skipped.append((ordinal, kind))
            pos += len(raw)
            continue
        return None
    if not skipped:
        return None
    nxt = read_line_at(path, pos)
    if nxt is None or nxt[1].get("ordinal") != next_ordinal:
        return None
    return Repair(
        kind="dup_skip",
        thread_id="",
        jsonl=path,
        old_offset=offset,
        old_ordinal=next_ordinal,
        new_offset=pos,
        new_ordinal=next_ordinal,
        skipped=skipped,
        detail="duplicate metadata ordinal",
    )


def classify(path: Path, offset: int, next_ordinal: int, last_ord: int | None, has_dup: bool) -> Repair | str:
    size = path.stat().st_size
    backfill = plan_ordinal_backfill(path, offset, next_ordinal)
    if offset == size:
        if backfill is not None:
            return backfill
        if isinstance(last_ord, int) and next_ordinal == last_ord + 1:
            return "healthy_eof"
        if isinstance(last_ord, int) and next_ordinal <= last_ord:
            return Repair(
                kind="ordinal_advance",
                thread_id="",
                jsonl=path,
                old_offset=offset,
                old_ordinal=next_ordinal,
                new_offset=offset,
                new_ordinal=last_ord + 1,
                detail=f"eof but ordinal behind last={last_ord}",
            )
        return "healthy_eof"
    if offset > size:
        return "offset_past_eof"

    first = read_line_at(path, offset)
    if first is None:
        return "mid_record_or_invalid"
    raw, rec = first
    ordinal = rec.get("ordinal")
    kind = payload_kind(rec)
    if not isinstance(ordinal, int):
        if backfill is not None:
            return backfill
        return "missing_ordinal"

    dup = plan_dup_skip(path, offset, next_ordinal)
    if dup is not None:
        return dup

    if ordinal == next_ordinal + 1:
        return Repair(
            kind="ordinal_advance",
            thread_id="",
            jsonl=path,
            old_offset=offset,
            old_ordinal=next_ordinal,
            new_offset=offset,
            new_ordinal=ordinal,
            detail=f"expected {next_ordinal}, file has {ordinal}",
        )

    prev = previous_line(path, offset)
    if prev is not None:
        prev_ord = prev[1].get("ordinal")
        prev_kind = payload_kind(prev[1])
        if (
            prev_kind == "token_count"
            and prev_ord == next_ordinal
            and ordinal == next_ordinal + 1
        ):
            return Repair(
                kind="ordinal_advance",
                thread_id="",
                jsonl=path,
                old_offset=offset,
                old_ordinal=next_ordinal,
                new_offset=offset,
                new_ordinal=ordinal,
                detail="byte cursor passed token_count without advancing ordinal",
            )

    if ordinal == next_ordinal:
        nxt = read_line_at(path, offset + len(raw))
        nxt_ord = nxt[1].get("ordinal") if nxt else None
        if kind == "token_count" and nxt_ord == next_ordinal + 1:
            return Repair(
                kind="skip_token_count",
                thread_id="",
                jsonl=path,
                old_offset=offset,
                old_ordinal=next_ordinal,
                new_offset=offset + len(raw),
                new_ordinal=next_ordinal + 1,
                skipped=[(ordinal, kind)],
                detail=f"skip unprojectable token_count {ordinal}",
            )
        return "healthy_can_catch_up"

    return f"unexpected_ordinal:{ordinal}:{kind}"


def load_cursors(db: Path) -> list[CursorState]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT thread_id, next_rollout_byte_offset, next_rollout_ordinal FROM thread_history_projection_state"
        ).fetchall()
    finally:
        con.close()
    out: list[CursorState] = []
    for thread_id, offset, nxt in rows:
        if isinstance(thread_id, str) and isinstance(offset, int) and isinstance(nxt, int):
            out.append(CursorState(thread_id=thread_id, offset=offset, next_ordinal=nxt))
    return out


def find_trailing_ordinal_less_suffix(path: Path) -> tuple[int, int, int] | None:
    """If the file ends with ordinal-less records, return (start, prev_ord, count)."""
    size = path.stat().st_size
    if size == 0:
        return None
    # Fast path: inspect the last non-empty line. If it has an ordinal,
    # the file cannot possibly end with an ordinal-less suffix.
    bufsize = min(size, 8192)
    with path.open("rb") as handle:
        handle.seek(max(0, size - bufsize))
        chunk = handle.read(bufsize)
    lines = [line.strip() for line in chunk.split(b"\n") if line.strip()]
    if lines:
        try:
            last_rec = json.loads(lines[-1].decode("utf-8"))
            if isinstance(last_rec, dict) and isinstance(last_rec.get("ordinal"), int):
                return None
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    suffix_start: int | None = None
    prev_ordinal: int | None = None
    count = 0
    with path.open("rb") as handle:
        while True:
            pos = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            if not isinstance(rec, dict):
                return None
            ordinal = rec.get("ordinal")
            if isinstance(ordinal, int):
                if suffix_start is not None:
                    return None
                prev_ordinal = ordinal
            else:
                if suffix_start is None:
                    suffix_start = pos
                    count = 0
                count += 1
    if suffix_start is None or prev_ordinal is None or count < 1:
        return None
    return suffix_start, prev_ordinal, count


def record_with_ordinal(rec: dict[str, Any], ordinal: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "timestamp" in rec:
        out["timestamp"] = rec["timestamp"]
    out["ordinal"] = ordinal
    for key, value in rec.items():
        if key in {"timestamp", "ordinal"}:
            continue
        out[key] = value
    return out


def preview_backfill_projection(path: Path, suffix_start: int, first_ordinal: int) -> tuple[int, int]:
    n_items = 0
    n_turns = 0
    ordinal = first_ordinal
    last_turn_id: str | None = None
    with path.open("rb") as handle:
        handle.seek(suffix_start)
        while True:
            raw = handle.readline()
            if not raw:
                break
            if not raw.strip():
                continue
            rec = json.loads(raw.decode("utf-8"))
            if not isinstance(rec, dict):
                break
            rec = record_with_ordinal(rec, ordinal)
            turns, items, last_turn_id = project_event(rec, last_turn_id)
            n_turns += len(turns)
            n_items += len(items)
            ordinal += 1
    return n_items, n_turns


def plan_ordinal_backfill(path: Path, offset: int, next_ordinal: int) -> Repair | None:
    found = find_trailing_ordinal_less_suffix(path)
    if found is None:
        return None
    suffix_start, prev_ord, count = found
    if prev_ord + 1 != next_ordinal:
        return None
    size = path.stat().st_size
    if not (suffix_start <= offset <= size):
        return None
    n_items, n_turns = preview_backfill_projection(path, suffix_start, next_ordinal)
    return Repair(
        kind="ordinal_backfill",
        thread_id="",
        jsonl=path,
        old_offset=suffix_start,
        old_ordinal=next_ordinal,
        new_offset=suffix_start,
        new_ordinal=next_ordinal + count,
        lines_rewritten=count,
        items_added=n_items,
        turns_upserted=n_turns,
        detail=f"backfill {count} ordinal-less lines {next_ordinal}->{next_ordinal + count - 1}",
    )


def apply_jsonl_ordinal_backfill(item: Repair) -> None:
    path = item.jsonl
    start = item.old_offset
    ordinal = item.old_ordinal
    tmp = path.with_name(path.name + ".ordinal_backfill.tmp")
    written = 0
    try:
        with path.open("rb") as src, tmp.open("wb") as dst:
            remaining = start
            while remaining:
                chunk = src.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError(f"{path}: truncated while copying prefix")
                dst.write(chunk)
                remaining -= len(chunk)
            while True:
                raw = src.readline()
                if not raw:
                    break
                if not raw.strip():
                    dst.write(raw)
                    continue
                rec = json.loads(raw.decode("utf-8"))
                if not isinstance(rec, dict):
                    raise RuntimeError(f"{path}: non-object while backfilling")
                if isinstance(rec.get("ordinal"), int):
                    raise RuntimeError(f"{path}: unexpected ordinal while backfilling")
                rec = record_with_ordinal(rec, ordinal)
                dst.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
                ordinal += 1
                written += 1
        if written == 0:
            raise RuntimeError(f"{path}: no ordinal-less lines to backfill")
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    item.lines_rewritten = written
    item.new_ordinal = ordinal


def backup_db(home: Path, extra_files: list[Path] | None = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = home / f"thread_history_backup_unwedge_{stamp}"
    dest.mkdir(parents=True, exist_ok=False)
    for name in (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm"):
        src = home / name
        if src.exists():
            shutil.copy2(src, dest / name)
    extra_dir = dest / "jsonl"
    seen: set[Path] = set()
    for src in extra_files or []:
        resolved = src.resolve()
        if resolved in seen or not src.is_file():
            continue
        seen.add(resolved)
        extra_dir.mkdir(exist_ok=True)
        shutil.copy2(src, extra_dir / src.name)
    return dest


def apply_repairs(db: Path, repairs: list[Repair]) -> None:
    for item in repairs:
        if item.kind == "ordinal_backfill":
            apply_jsonl_ordinal_backfill(item)
    catch_up_ids = {item.thread_id for item in repairs if item.kind == "catch_up"}
    sqlite_repairs = [
        item
        for item in repairs
        if not (item.kind == "ordinal_backfill" and item.thread_id in catch_up_ids)
    ]
    con = sqlite3.connect(str(db), timeout=30)
    try:
        cur = con.cursor()
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("BEGIN IMMEDIATE")
        for item in sqlite_repairs:
            if item.kind == "rebuild":
                con.rollback()
                raise RuntimeError(
                    f"{item.thread_id}: rebuild-by-delete is disabled because Desktop does not rematerialize"
                )
            elif item.kind in {"dup_skip", "skip_token_count"}:
                fields_offset = item.new_offset
                fields_ordinal = item.new_ordinal if item.kind == "skip_token_count" else item.old_ordinal
                cur.execute(
                    """
                    UPDATE thread_history_projection_state
                    SET next_rollout_byte_offset = ?,
                        next_rollout_ordinal = ?
                    WHERE thread_id = ?
                      AND next_rollout_byte_offset = ?
                      AND next_rollout_ordinal = ?
                    """,
                    (fields_offset, fields_ordinal, item.thread_id, item.old_offset, item.old_ordinal),
                )
                if cur.rowcount != 1:
                    con.rollback()
                    raise RuntimeError(f"{item.thread_id}: {item.kind} affected {cur.rowcount} rows")
            elif item.kind == "ordinal_advance":
                cur.execute(
                    """
                    UPDATE thread_history_projection_state
                    SET next_rollout_ordinal = ?
                    WHERE thread_id = ?
                      AND next_rollout_byte_offset = ?
                      AND next_rollout_ordinal = ?
                    """,
                    (item.new_ordinal, item.thread_id, item.old_offset, item.old_ordinal),
                )
                if cur.rowcount != 1:
                    con.rollback()
                    raise RuntimeError(f"{item.thread_id}: ordinal_advance affected {cur.rowcount} rows")
            elif item.kind == "catch_up":
                try:
                    new_offset, new_ordinal, n_items, n_turns = apply_catch_up(cur, item)
                except Exception:
                    con.rollback()
                    raise
                item.new_offset = new_offset
                item.new_ordinal = new_ordinal
                item.items_added = n_items
                item.turns_upserted = n_turns
            elif item.kind == "ordinal_backfill":
                try:
                    new_offset, new_ordinal, n_items, n_turns = apply_catch_up(
                        cur, item, replay_from_item=True
                    )
                except Exception:
                    con.rollback()
                    raise
                item.new_offset = new_offset
                item.new_ordinal = new_ordinal
                item.items_added = n_items
                item.turns_upserted = n_turns
            else:
                con.rollback()
                raise RuntimeError(f"{item.thread_id}: unknown repair {item.kind}")
        con.commit()
        try:
            cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
            cur.fetchone()
        except sqlite3.Error:
            pass
    finally:
        con.close()


def append_log(home: Path, lines: list[str]) -> None:
    log = home / "unwedge.log"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}]\n")
        for line in lines:
            handle.write(line + "\n")
        handle.write("\n")


def plan_catch_up(
    path: Path, offset: int, next_ordinal: int, thread_id: str
) -> Repair | None:
    size = path.stat().st_size
    if offset >= size:
        return None
    first = read_line_at(path, offset)
    if first is None:
        return None
    ordinal = first[1].get("ordinal")
    if ordinal != next_ordinal:
        return None
    turns, items, new_offset, new_ordinal, lines = replay_suffix(path, offset, next_ordinal)
    if new_offset == offset and not turns and not items:
        return None
    return Repair(
        kind="catch_up",
        thread_id=thread_id,
        jsonl=path,
        old_offset=offset,
        old_ordinal=next_ordinal,
        new_offset=new_offset,
        new_ordinal=new_ordinal,
        items_added=len(items),
        turns_upserted=len(turns),
        detail=f"{len(items)} items, {len(turns)} turn events, {lines} lines, {offset}->{new_offset}",
    )


def repair_home(
    home: Path,
    dry_run: bool,
    force_locked: bool,
    host_label: str,
    catch_up: bool = True,
    thread_ids: list[str] | None = None,
) -> tuple[int, dict[str, Any], list[str]]:
    lines: list[str] = []
    db = home / DB_NAME
    summary = {
        "host": host_label,
        "codex_home": str(home),
        "projection_threads": 0,
        "healthy": 0,
        "no_jsonl": 0,
        "dup_skip": 0,
        "ordinal_advance": 0,
        "skip_token_count": 0,
        "ordinal_backfill": 0,
        "ordinal_backfill_lines": 0,
        "catch_up": 0,
        "catch_up_items": 0,
        "rebuild": 0,
        "dry_run": bool(dry_run),
        "applied": 0,
    }
    if not db.exists():
        lines.append(f"missing db: {db}")
        return 1, summary, lines

    locked = not sqlite_unlocked(db)
    if locked and not dry_run and not force_locked:
        lines.append("Codex 仍占用 thread_history_1.sqlite。请先完全退出后再运行。")
        return 2, summary, lines

    index = index_rollouts(home)
    cursors = load_cursors(db)
    if thread_ids:
        wanted = {item.lower() for item in thread_ids}
        cursors = [item for item in cursors if item.thread_id.lower() in wanted]
    inspect_cache: dict[Path, tuple[int | None, bool]] = {}
    repairs: list[Repair] = []
    notes: list[str] = []
    n_healthy = 0
    n_no_jsonl = 0
    healthy_ids: set[str] = set()
    post_cursors: dict[str, tuple[Path, int, int]] = {}
    for cursor in cursors:
        candidates = index.get(cursor.thread_id.lower(), [])
        if not candidates:
            n_no_jsonl += 1
            continue
        path = pick_jsonl(candidates, cursor.offset)
        if path is None:
            n_no_jsonl += 1
            continue
        if path not in inspect_cache:
            inspect_cache[path] = inspect_jsonl(path)
        last_ord, has_dup = inspect_cache[path]
        result = classify(path, cursor.offset, cursor.next_ordinal, last_ord, has_dup)
        post_offset, post_ordinal = cursor.offset, cursor.next_ordinal
        if isinstance(result, Repair):
            result.thread_id = cursor.thread_id
            repairs.append(result)
            notes.append(f"{cursor.thread_id}: {result.kind}  {result.detail}  {path.name}")
            if result.new_offset is not None:
                post_offset = result.new_offset
            if result.new_ordinal is not None:
                post_ordinal = result.new_ordinal
        elif result in {"healthy_eof", "healthy_can_catch_up"}:
            n_healthy += 1
            healthy_ids.add(cursor.thread_id)
        else:
            notes.append(f"{cursor.thread_id}: {result}  {path.name}")
        post_cursors[cursor.thread_id] = (path, post_offset, post_ordinal)

    if catch_up:
        extra_backfills: list[Repair] = []
        already_backfill = {
            item.thread_id for item in repairs if item.kind == "ordinal_backfill"
        }
        for thread_id, (path, offset, nxt) in post_cursors.items():
            if thread_id in already_backfill:
                continue
            planned = plan_catch_up(path, offset, nxt, thread_id)
            if planned is None:
                backfill = plan_ordinal_backfill(path, offset, nxt)
                if backfill is not None:
                    backfill.thread_id = thread_id
                    extra_backfills.append(backfill)
                    notes.append(
                        f"{thread_id}: {backfill.kind}  {backfill.detail}  {path.name}"
                    )
                    already_backfill.add(thread_id)
                continue
            repairs.append(planned)
            notes.append(f"{thread_id}: catch_up  {planned.detail}  {path.name}")
            if thread_id in healthy_ids:
                healthy_ids.discard(thread_id)
                n_healthy = max(0, n_healthy - 1)
            if planned.new_offset is not None and planned.new_ordinal is not None:
                backfill = plan_ordinal_backfill(
                    path, planned.new_offset, planned.new_ordinal
                )
                if backfill is not None:
                    backfill.thread_id = thread_id
                    extra_backfills.append(backfill)
                    notes.append(
                        f"{thread_id}: {backfill.kind}  {backfill.detail}  {path.name}"
                    )
                    already_backfill.add(thread_id)
        repairs.extend(extra_backfills)

    repairs.sort(
        key=lambda item: (
            0 if item.kind == "ordinal_backfill" else 1 if item.kind != "catch_up" else 2,
            item.thread_id,
        )
    )

    n_dup = sum(1 for item in repairs if item.kind == "dup_skip")
    n_adv = sum(1 for item in repairs if item.kind == "ordinal_advance")
    n_skip_tc = sum(1 for item in repairs if item.kind == "skip_token_count")
    n_backfill = sum(1 for item in repairs if item.kind == "ordinal_backfill")
    n_backfill_lines = sum(
        item.lines_rewritten for item in repairs if item.kind == "ordinal_backfill"
    )
    n_catch = sum(1 for item in repairs if item.kind == "catch_up")
    n_items = sum(item.items_added for item in repairs if item.kind == "catch_up")
    n_items += sum(item.items_added for item in repairs if item.kind == "ordinal_backfill")
    summary.update(
        {
            "projection_threads": len(cursors),
            "healthy": n_healthy,
            "no_jsonl": n_no_jsonl,
            "dup_skip": n_dup,
            "ordinal_advance": n_adv,
            "skip_token_count": n_skip_tc,
            "ordinal_backfill": n_backfill,
            "ordinal_backfill_lines": n_backfill_lines,
            "catch_up": n_catch,
            "catch_up_items": n_items,
            "rebuild": 0,
        }
    )
    lines.append(f"projection threads: {len(cursors)}")
    lines.append(f"healthy: {n_healthy}")
    lines.append(f"no matching jsonl: {n_no_jsonl}")
    lines.append(f"duplicate wedges: {n_dup}")
    lines.append(f"ordinal advances: {n_adv}")
    lines.append(f"token_count skips: {n_skip_tc}")
    lines.append(f"ordinal backfills: {n_backfill} ({n_backfill_lines} lines)")
    lines.append(f"history catch-ups: {n_catch} ({n_items} items)")
    if notes:
        lines.append("notes:")
        lines.extend(f"  {note}" for note in notes)
    if not repairs:
        lines.append("没有发现可自动修复的投影卡点。")

    if dry_run or not repairs:
        append_log(home, ["dry-run" if dry_run else "no-op", *notes[:80]])
        return 0, summary, lines

    jsonl_files = [item.jsonl for item in repairs if item.kind == "ordinal_backfill"]
    dest = backup_db(home, jsonl_files)
    lines.append(f"backup: {dest}")
    apply_repairs(db, repairs)
    summary["applied"] = len(repairs)
    summary["backup"] = str(dest)
    lines.append(f"applied: {len(repairs)}")
    append_log(home, [f"backup {dest}", *notes])
    return 0, summary, lines


def emit_result(title: str, code: int, summary: dict[str, Any], lines: list[str]) -> None:
    _print(f"==== {title} ====")
    for line in lines:
        _print(line)
    summary = dict(summary)
    summary["exit_code"] = code
    _print("SUMMARY " + json.dumps(summary, ensure_ascii=False))


def ssh_command(host: str, remote: str) -> list[str]:
    return [
        "ssh",
        "-o", "ClearAllForwardings=yes",
        "-o", "LogLevel=ERROR",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=12",
        host,
        remote,
    ]


def run_via_ssh(
    host: str,
    dry_run: bool,
    force_locked: bool,
    catch_up: bool,
    thread_ids: list[str] | None = None,
) -> tuple[int, str]:
    source = Path(__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
    remote = 'python3 - --codex-home "$HOME/.codex" --host-label remote'
    if dry_run:
        remote += " --dry-run"
    if force_locked:
        remote += " --force-locked"
    if not catch_up:
        remote += " --no-catch-up"
    if thread_ids:
        for thread_id in thread_ids:
            remote += f" --thread-id {thread_id}"
    cmd = ssh_command(host, remote)
    try:
        proc = subprocess.run(
            cmd,
            input=source,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 3, str(exc)
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, out


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair wedged Codex history projections")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不改 SQLite")
    parser.add_argument("--force-locked", action="store_true", help="数据库仍被占用时也尝试写入（不推荐）")
    parser.add_argument("--ssh-host", help="通过 SSH 在该主机上执行同一修复，例如 server-codex")
    parser.add_argument("--remote-only", action="store_true", help="只修 SSH 主机，不改本机")
    parser.add_argument("--host-label", default="local", help="SUMMARY 里的 host 字段")
    parser.add_argument("--no-catch-up", action="store_true", help="只修游标，不把 JSONL 后缀写入 sqlite")
    parser.add_argument(
        "--thread-id",
        action="append",
        default=None,
        help="只处理这些 thread id，可重复。默认处理投影表里的全部会话",
    )
    args = parser.parse_args()
    do_catch_up = not args.no_catch_up

    codes: list[int] = []
    ssh_future = None
    executor = None
    if args.ssh_host:
        from concurrent.futures import ThreadPoolExecutor

        executor = ThreadPoolExecutor(max_workers=1)
        ssh_future = executor.submit(
            run_via_ssh,
            args.ssh_host,
            args.dry_run,
            args.force_locked,
            do_catch_up,
            args.thread_id,
        )

    if not args.remote_only:
        code, summary, lines = repair_home(
            args.codex_home.expanduser(),
            args.dry_run,
            args.force_locked,
            args.host_label,
            catch_up=do_catch_up,
            thread_ids=args.thread_id,
        )
        emit_result(args.host_label, code, summary, lines)
        codes.append(code)

    if ssh_future is not None:
        remote_code, remote_out = ssh_future.result()
        if executor is not None:
            executor.shutdown(wait=False)
        _print(f"==== remote {args.ssh_host} ====")
        if remote_out.strip():
            _print(remote_out.rstrip())
        else:
            _print(f"ssh failed with exit {remote_code}")
        if remote_code != 0 and "SUMMARY " not in remote_out:
            emit_result(
                f"remote {args.ssh_host}",
                remote_code,
                {"host": args.ssh_host, "dup_skip": 0, "ordinal_advance": 0, "ordinal_backfill": 0, "ordinal_backfill_lines": 0, "catch_up": 0, "rebuild": 0, "dry_run": args.dry_run, "applied": 0},
                [remote_out.strip() or f"ssh exit {remote_code}"],
            )
        codes.append(remote_code)

    if not codes:
        parser.error("请指定本机修复或 --ssh-host")
    if 0 in codes:
        return 0
    return codes[-1]


if __name__ == "__main__":
    raise SystemExit(main())
