#!/usr/bin/env python3
"""Stitch Codex rollout volumes and open a complete history viewer.

Codex Desktop's SQLite projection can freeze after thread/revert or a
duplicate ordinal. This script does not patch Codex. It already stitches
every volume into one timeline, then writes a single HTML/Markdown/JSONL.

Do not copy the JSONL archive back into ~/.codex/sessions.

Examples:

    python view_codex_thread.py 01a037fd-4841-7373-ad1a-c2a1e0292936
    python view_codex_thread.py 01a037fd-4841-7373-ad1a-c2a1e0292936 --md --jsonl
    python view_codex_thread.py --list
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

CST = timezone(timedelta(hours=8))
DEFAULT_SESSION_ROOTS = (
    Path.home() / ".codex" / "sessions",
    Path.home() / ".codex" / "archived_sessions",
)
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


@dataclass
class RolloutInfo:
    path: Path
    thread_id: str
    rollout_id: str
    history_base_id: str | None
    cut: int | None
    first_ts: str | None
    last_ts: str | None
    first_ord: int | None
    last_ord: int | None
    n_lines: int = 0


def parse_thread_id(raw: str) -> str:
    text = raw.strip()
    text = text.replace("codex://threads/", "")
    text = text.split("?", 1)[0].strip().strip("/")
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    match = UUID_RE.search(text)
    if not match:
        raise ValueError(f"无法解析 thread id: {raw}")
    return str(UUID(match.group(0)))


def rollout_id_from_name(path: Path) -> str | None:
    name = path.name
    if name.startswith("rollout-") and name.endswith(".jsonl"):
        name = name[len("rollout-") : -len(".jsonl")]
    matches = UUID_RE.findall(name)
    return matches[-1].lower() if matches else None


def to_cst(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(CST)
    except ValueError:
        return ts
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def iter_rollout_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("rollout-*.jsonl"):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(path)
    if not files:
        searched = ", ".join(str(root) for root in roots)
        raise FileNotFoundError(f"找不到 Codex rollout 文件: {searched}")
    return sorted(files)


def read_header(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            line = handle.readline()
    except OSError:
        return None
    if not line.strip():
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def read_last_record(path: Path) -> dict[str, Any] | None:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        pos = handle.tell()
        if pos == 0:
            return None
        buf = b""
        while pos > 0:
            step = min(4096, pos)
            pos -= step
            handle.seek(pos)
            buf = handle.read(step) + buf
            if buf.count(b"\n") >= 2 or pos == 0:
                break
    for raw in reversed(buf.splitlines()):
        if not raw.strip():
            continue
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return None


def inspect_rollout(path: Path) -> RolloutInfo | None:
    header = read_header(path)
    if not header:
        return None
    payload = header.get("payload") if isinstance(header.get("payload"), dict) else {}
    thread_id = str(payload.get("id") or payload.get("session_id") or "").lower()
    rid = (rollout_id_from_name(path) or thread_id).lower()
    if not thread_id and not rid:
        return None
    base = payload.get("history_base") if isinstance(payload.get("history_base"), dict) else {}
    base_id = str(base.get("thread_id") or "").lower() or None
    cut = base.get("end_ordinal_exclusive")
    if not isinstance(cut, int):
        cut = None
    last = read_last_record(path) or header
    first_ord = header.get("ordinal") if isinstance(header.get("ordinal"), int) else None
    last_ord = last.get("ordinal") if isinstance(last.get("ordinal"), int) else first_ord
    return RolloutInfo(
        path=path,
        thread_id=thread_id or rid,
        rollout_id=rid or thread_id,
        history_base_id=base_id,
        cut=cut,
        first_ts=header.get("timestamp"),
        last_ts=last.get("timestamp") or header.get("timestamp"),
        first_ord=first_ord,
        last_ord=last_ord,
        n_lines=0,
    )


def index_rollouts(roots: list[Path]) -> list[RolloutInfo]:
    items: list[RolloutInfo] = []
    for path in iter_rollout_files(roots):
        info = inspect_rollout(path)
        if info:
            items.append(info)
    return items


def collect_lineage(target: str, catalog: list[RolloutInfo]) -> list[RolloutInfo]:
    by_rollout = {item.rollout_id: item for item in catalog}
    by_thread: dict[str, list[RolloutInfo]] = {}
    for item in catalog:
        by_thread.setdefault(item.thread_id, []).append(item)

    selected: dict[str, RolloutInfo] = {}
    stack = list(by_thread.get(target, []))
    if target in by_rollout:
        stack.append(by_rollout[target])
    for item in catalog:
        if target in item.path.name.lower():
            stack.append(item)

    while stack:
        item = stack.pop()
        if item.rollout_id in selected:
            continue
        selected[item.rollout_id] = item
        if item.history_base_id:
            parent = by_rollout.get(item.history_base_id)
            if parent is None:
                stack.extend(by_thread.get(item.history_base_id, []))
            else:
                stack.append(parent)
        for child in catalog:
            if child.history_base_id in {item.rollout_id, item.thread_id, target}:
                if child.rollout_id not in selected:
                    stack.append(child)

    if not selected:
        raise FileNotFoundError(f"没有找到 thread {target} 的 rollout 文件")

    children_of = {item.history_base_id for item in selected.values() if item.history_base_id}
    tips = [
        item
        for item in selected.values()
        if item.rollout_id not in children_of and item.thread_id not in children_of
    ]
    if not tips:
        tips = list(selected.values())
    tip = max(tips, key=lambda item: (item.last_ts or "", item.n_lines, item.path.name))

    chain: list[RolloutInfo] = []
    seen: set[str] = set()
    cur: RolloutInfo | None = tip
    while cur is not None and cur.rollout_id not in seen:
        chain.append(cur)
        seen.add(cur.rollout_id)
        parent_id = cur.history_base_id
        cur = by_rollout.get(parent_id) if parent_id else None
        if cur is None and parent_id:
            parents = [item for item in selected.values() if item.rollout_id == parent_id or item.thread_id == parent_id]
            cur = parents[-1] if parents else None
    chain.reverse()
    return chain


def load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_ord: set[int] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ordinal = obj.get("ordinal")
            if isinstance(ordinal, int):
                if ordinal in seen_ord:
                    continue
                seen_ord.add(ordinal)
            records.append(obj)
    return records


def stitch(chain: list[RolloutInfo]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for index, info in enumerate(chain):
        next_cut = chain[index + 1].cut if index + 1 < len(chain) else None
        for record in load_records(info.path):
            ordinal = record.get("ordinal")
            if next_cut is not None and isinstance(ordinal, int) and ordinal >= next_cut:
                continue
            record["_rollout"] = info.path.name
            merged.append(record)
    return merged


def content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("text"):
                    parts.append(str(item["text"]))
                elif item.get("type") in {"input_image", "image"}:
                    parts.append("[图片]")
        return "\n".join(parts)
    if isinstance(value, dict):
        return content_text(value.get("text") or value.get("content"))
    return str(value)


def clean_user_text(text: str) -> str:
    text = re.sub(r"<environment_context>[\s\S]*?</environment_context>", "", text)
    if "## My request:" in text:
        text = text.split("## My request:", 1)[-1]
    text = re.sub(
        r"## Referenced chats with Codex:[\s\S]*?(?=\n## My request:|\Z)",
        "",
        text,
    )
    return text.strip()


def _item_text(item: dict[str, Any]) -> str:
    if item.get("text"):
        return str(item.get("text") or "")
    return content_text(item.get("content") or item.get("message"))


def extract_events(records: list[dict[str, Any]], include_tools: bool) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for record in records:
        rtype = record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        ptype = payload.get("type")
        ts = record.get("timestamp")
        ordinal = record.get("ordinal")
        rollout = record.get("_rollout")
        if rtype == "session_meta":
            continue
        if rtype == "compacted":
            events.append(
                {
                    "kind": "system",
                    "ts": ts,
                    "ordinal": ordinal,
                    "rollout": rollout,
                    "title": "上下文压缩",
                    "text": "模型上下文已压缩。磁盘日志仍保留完整记录。",
                }
            )
            continue
        if rtype == "response_item" and ptype == "message":
            role = payload.get("role")
            text = content_text(payload.get("content"))
            if role == "user":
                text = clean_user_text(text)
            if role in {"user", "assistant"} and text:
                events.append(
                    {
                        "kind": role,
                        "ts": ts,
                        "ordinal": ordinal,
                        "rollout": rollout,
                        "text": text,
                    }
                )
            continue
        if rtype == "event_msg" and ptype == "item_completed":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            itype = str(item.get("type") or "")
            if itype in {"UserMessage", "userMessage"}:
                text = clean_user_text(_item_text(item))
                if text:
                    events.append(
                        {
                            "kind": "user",
                            "ts": ts,
                            "ordinal": ordinal,
                            "rollout": rollout,
                            "text": text,
                        }
                    )
            elif itype in {"AgentMessage", "agentMessage"}:
                text = _item_text(item).strip()
                if text:
                    events.append(
                        {
                            "kind": "assistant",
                            "ts": ts,
                            "ordinal": ordinal,
                            "rollout": rollout,
                            "text": text,
                            "final": str(item.get("phase") or "") in {"", "final", "final_answer"},
                        }
                    )
            continue
        if rtype == "event_msg" and ptype in {"user_message", "UserMessage"}:
            text = clean_user_text(str(payload.get("message") or payload.get("text") or ""))
            if text:
                events.append(
                    {
                        "kind": "user",
                        "ts": ts,
                        "ordinal": ordinal,
                        "rollout": rollout,
                        "text": text,
                    }
                )
            continue
        if rtype == "event_msg" and ptype in {"agent_message", "AgentMessage"}:
            text = str(payload.get("message") or payload.get("text") or "").strip()
            if text:
                events.append(
                    {
                        "kind": "assistant",
                        "ts": ts,
                        "ordinal": ordinal,
                        "rollout": rollout,
                        "text": text,
                        "final": True,
                    }
                )
            continue
        if rtype == "event_msg" and ptype == "task_complete":
            text = str(payload.get("last_agent_message") or "").strip()
            if text:
                events.append(
                    {
                        "kind": "assistant",
                        "ts": ts,
                        "ordinal": ordinal,
                        "rollout": rollout,
                        "text": text,
                        "final": True,
                    }
                )
            err = payload.get("error")
            if isinstance(err, dict):
                err_text = str(err.get("message") or err).strip()
            else:
                err_text = str(err or "").strip()
            if err_text:
                events.append(
                    {
                        "kind": "system",
                        "ts": ts,
                        "ordinal": ordinal,
                        "rollout": rollout,
                        "title": "本轮失败",
                        "text": err_text,
                    }
                )
            continue
        if rtype == "event_msg" and ptype == "turn_aborted":
            events.append(
                {
                    "kind": "system",
                    "ts": ts,
                    "ordinal": ordinal,
                    "rollout": rollout,
                    "title": "本轮被中断",
                    "text": str(payload.get("reason") or "interrupted"),
                }
            )
            continue
        if include_tools and rtype == "response_item" and ptype in {
            "custom_tool_call",
            "function_call",
        }:
            name = payload.get("name") or payload.get("tool") or ptype
            args = payload.get("input") or payload.get("arguments") or payload.get("params")
            preview = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)[:2000]
            events.append(
                {
                    "kind": "tool",
                    "ts": ts,
                    "ordinal": ordinal,
                    "rollout": rollout,
                    "title": str(name),
                    "text": preview,
                }
            )
    collapsed: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        kind = str(event.get("kind") or "")
        text = str(event.get("text") or "")
        if kind in {"user", "assistant"}:
            key = (kind, text)
            if key in seen:
                continue
            seen.add(key)
        if (
            collapsed
            and event.get("kind") == "assistant"
            and event.get("final")
            and collapsed[-1].get("kind") == "assistant"
            and collapsed[-1].get("text") == event.get("text")
        ):
            continue
        collapsed.append(event)
    return collapsed


def render_html(
    thread_id: str,
    chain: list[RolloutInfo],
    events: list[dict[str, Any]],
) -> str:
    cards: list[str] = []
    for event in events:
        kind = event["kind"]
        label = {
            "user": "用户",
            "assistant": "Codex",
            "tool": "工具",
            "system": "系统",
        }.get(kind, kind)
        meta = f'{to_cst(event.get("ts"))} · ordinal {event.get("ordinal")}'
        body = html.escape(event.get("text") or "")
        title = html.escape(event.get("title") or "")
        extra = f"<div class='title'>{title}</div>" if title else ""
        cards.append(
            f"<article class='msg {html.escape(kind)}'>"
            f"<header><span>{label}</span><span>{html.escape(meta)}</span></header>"
            f"{extra}<pre>{body}</pre></article>"
        )
    files = "<ol>" + "".join(
        (
            "<li>"
            f"<code>{html.escape(item.path.name)}</code>"
            f"<div>{to_cst(item.first_ts)} → {to_cst(item.last_ts)} · "
            f"ordinal {item.first_ord}–{item.last_ord} · {item.n_lines} 条</div>"
            "</li>"
        )
        for item in chain
    ) + "</ol>"
    first_ts = to_cst(events[0]["ts"]) if events else ""
    last_ts = to_cst(events[-1]["ts"]) if events else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>Codex {html.escape(thread_id)}</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ margin: 0; font-family: "Segoe UI", sans-serif; background: #101114; color: #ececec; }}
    header.top {{ position: sticky; top: 0; background: #17181c; padding: 16px 24px; border-bottom: 1px solid #2a2b31; z-index: 2; }}
    h1 {{ font-size: 18px; margin: 0 0 8px; }}
    .meta, li div {{ color: #9aa0a6; font-size: 13px; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 20px 16px 64px; }}
    .msg {{ margin: 14px 0; padding: 12px 14px; border-radius: 12px; background: #1c1d22; }}
    .msg.user {{ background: #243044; }}
    .msg.assistant {{ background: #1a2420; }}
    .msg.system, .msg.tool {{ background: #222226; color: #c5c5c5; }}
    .msg header {{ display: flex; justify-content: space-between; font-size: 12px; color: #9aa0a6; margin-bottom: 8px; }}
    .title {{ font-weight: 600; margin-bottom: 6px; }}
    pre {{ white-space: pre-wrap; word-break: break-word; margin: 0; font-family: inherit; line-height: 1.45; }}
    input {{ width: 100%; max-width: 420px; margin-top: 10px; padding: 8px 10px; border-radius: 8px; border: 1px solid #3a3b42; background: #101114; color: inherit; }}
  </style>
</head>
<body>
  <header class="top">
    <h1>Codex 完整对话 · {html.escape(thread_id)}</h1>
    <div class="meta">已拼接为 <b>1 条时间线</b> · 来源分卷 {len(chain)} 份 · 可见条目 {len(events)} · {html.escape(first_ts)} → {html.escape(last_ts)}</div>
    {files}
    <input id="q" placeholder="搜索原文（即时过滤）"/>
  </header>
  <main id="log">{''.join(cards)}</main>
  <script>
    const q = document.getElementById("q");
    q.addEventListener("input", () => {{
      const needle = q.value.trim().toLowerCase();
      for (const el of document.querySelectorAll("article.msg")) {{
        el.style.display = !needle || el.innerText.toLowerCase().includes(needle) ? "" : "none";
      }}
    }});
    window.scrollTo(0, document.body.scrollHeight);
  </script>
</body>
</html>
"""


def render_markdown(thread_id: str, chain: list[RolloutInfo], events: list[dict[str, Any]]) -> str:
    lines = [
        f"# Codex 完整对话（已拼接）",
        "",
        f"- thread: `{thread_id}`",
        f"- 链接: `codex://threads/{thread_id}`",
        f"- 归档时间: {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} CST",
        f"- 来源分卷: {len(chain)} 份，已按 ordinal 去重后拼成 1 条时间线",
        f"- 可见条目: {len(events)}",
        "- 本文件是只读归档，不要拷回 `~/.codex/sessions`。",
        "",
        "## 分卷来源",
        "",
    ]
    for item in chain:
        lines.append(
            f"- `{item.path.name}`：{to_cst(item.first_ts)} → {to_cst(item.last_ts)}，"
            f"ordinal {item.first_ord}–{item.last_ord}"
        )
    lines.extend(["", "## 对话", ""])
    labels = {"user": "用户", "assistant": "Codex", "tool": "工具", "system": "系统"}
    for event in events:
        kind = labels.get(event["kind"], event["kind"])
        title = event.get("title")
        heading = f"{kind} · {to_cst(event.get('ts'))} · ordinal {event.get('ordinal')}"
        if title:
            heading += f" · {title}"
        lines.append(f"### {heading}")
        lines.append("")
        lines.append((event.get("text") or "").rstrip() or "_(空)_")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_stitched_jsonl(path: Path, thread_id: str, chain: list[RolloutInfo], records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for index, record in enumerate(records):
            obj = dict(record)
            obj.pop("_rollout", None)
            obj["ordinal"] = index
            if index == 0 and obj.get("type") == "session_meta":
                payload = dict(obj.get("payload") or {})
                payload["archive_note"] = (
                    "stitched-read-only-archive; do not copy into ~/.codex/sessions"
                )
                payload["archive_thread_id"] = thread_id
                payload["archive_source_files"] = [item.path.name for item in chain]
                obj["payload"] = payload
            handle.write(json.dumps(obj, ensure_ascii=False) + "\n")


def list_threads(catalog: list[RolloutInfo]) -> str:
    grouped: dict[str, list[RolloutInfo]] = {}
    for item in catalog:
        grouped.setdefault(item.thread_id, []).append(item)
    rows = []
    for thread_id, items in grouped.items():
        items = sorted(items, key=lambda x: x.last_ts or "")
        last = items[-1]
        rows.append(
            (
                last.last_ts or "",
                f"{thread_id}\tvolumes={len(items)}\t"
                f"{to_cst(items[0].first_ts)} -> {to_cst(last.last_ts)}",
            )
        )
    rows.sort(reverse=True)
    return "\n".join(row[1] for row in rows)


def _print(text: str) -> None:
    stream = sys.stdout
    encoding = stream.encoding or "utf-8"
    stream.write(text.encode(encoding, errors="replace").decode(encoding, errors="replace") + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild a Codex thread and open it in the browser")
    parser.add_argument("thread", nargs="?", help="thread id 或 codex://threads/...")
    parser.add_argument("--list", action="store_true", help="列出本机所有 thread 及分卷数")
    parser.add_argument(
        "--sessions",
        type=Path,
        action="append",
        help="额外 sessions 目录；默认同时扫描 ~/.codex/sessions 与 archived_sessions",
    )
    parser.add_argument("--tools", action="store_true", help="同时显示工具调用")
    parser.add_argument("--no-open", action="store_true", help="只生成文件，不打开浏览器")
    parser.add_argument("--out", type=Path, help="指定 HTML 输出路径")
    parser.add_argument("--md", action="store_true", help="再写一份拼接后的单个 Markdown")
    parser.add_argument("--md-out", type=Path, help="Markdown 输出路径；默认与 HTML 同名")
    parser.add_argument("--jsonl", action="store_true", help="再写一份拼接后的单个 JSONL 归档")
    parser.add_argument("--jsonl-out", type=Path, help="JSONL 输出路径；默认与 HTML 同名")
    args = parser.parse_args()

    roots = list(DEFAULT_SESSION_ROOTS)
    if args.sessions:
        roots.extend(args.sessions)
    catalog = index_rollouts(roots)
    if args.list:
        _print(list_threads(catalog))
        return 0
    if not args.thread:
        parser.error("请提供 thread id，或使用 --list")

    thread_id = parse_thread_id(args.thread)
    chain = collect_lineage(thread_id, catalog)
    records = stitch(chain)
    for info in chain:
        info.n_lines = sum(1 for rec in records if rec.get("_rollout") == info.path.name)
    events = extract_events(records, include_tools=args.tools)
    html_text = render_html(thread_id, chain, events)
    out_path = args.out or Path(tempfile.gettempdir()) / f"codex-thread-{thread_id}.html"
    out_path.write_text(html_text, encoding="utf-8")
    _print(f"volumes: {len(chain)}")
    for item in chain:
        _print(
            f"  {item.path.name}  {to_cst(item.first_ts)} -> {to_cst(item.last_ts)}  "
            f"ord {item.first_ord}-{item.last_ord}"
        )
    _print(f"visible events: {len(events)}")
    _print(f"stitched records: {len(records)}")
    _print(f"html: {out_path}")
    if args.md or args.md_out:
        md_path = args.md_out or out_path.with_suffix(".md")
        md_path.write_text(render_markdown(thread_id, chain, events), encoding="utf-8")
        _print(f"markdown: {md_path}")
    if args.jsonl or args.jsonl_out:
        jsonl_path = args.jsonl_out or out_path.with_suffix(".jsonl")
        write_stitched_jsonl(jsonl_path, thread_id, chain, records)
        _print(f"jsonl archive: {jsonl_path}")
        _print("note: jsonl archive is read-only; do not copy it into ~/.codex/sessions")
    if not args.no_open:
        webbrowser.open(out_path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
