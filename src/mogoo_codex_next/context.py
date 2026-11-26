"""从完成轮次提取用户请求与最终回答，不包含工具和推理输出。"""
from __future__ import annotations

import hashlib
import json
import re

MAX_CONTEXT_CHARS = 24000
RECENT_TURNS = 5


def _clean(text: str) -> str:
    for tag in ("environment_context", "in-app-browser-context", "skills_instructions",
                "recommended_plugins", "app-context", "external_codex_apps_open_page"):
        text = re.sub(r"<" + tag + r"\b[^>]*>[\s\S]*?</" + tag + r">", "", text)
    return text.strip()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    # 末尾常是待确认选项或阻塞信息，不能只截前缀。
    head = max(1, limit // 3)
    return text[:head] + "\n[…中间内容已省略…]\n" + text[-(limit - head - 18):]


def _messages(turn: dict) -> list:
    output = []
    for item in turn.get("items", []):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "userMessage":
            text = "\n".join(p.get("text", "") for p in item.get("content", [])
                             if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
            role = "user"
        elif kind == "agentMessage" and item.get("phase") in (None, "final_answer"):
            text, role = item.get("text", ""), "assistant"
        else:
            continue
        if isinstance(text, str) and (text := _clean(text)):
            output.append({"role": role, "content": text})
    return output


def snapshot(thread: dict) -> dict | None:
    # 某些宿主先更新线程状态，再持久化新 turn；此时旧 completed 不能触发建议。
    # notLoaded 是独立只读 App Server 读取持久化线程的合法状态，不代表原线程繁忙。
    status = thread.get("status")
    status_type = status.get("type") if isinstance(status, dict) else status
    if status_type not in (None, "idle", "notLoaded"):
        return None
    if thread.get("archived") is True or thread.get("isArchived") is True:
        return None
    turns = thread.get("turns", [])
    if not turns or not isinstance(turns[-1], dict) or turns[-1].get("status") != "completed":
        return None
    latest = turns[-1]
    if latest.get("itemsView", "full") != "full" or not thread.get("id") or not latest.get("id"):
        return None
    completed = [(turn, _messages(turn)) for turn in turns if isinstance(turn, dict)
                 and turn.get("status") == "completed" and turn.get("itemsView", "full") == "full"]
    last_messages = _messages(latest)
    if not any(m["role"] == "assistant" for m in last_messages):
        return None
    recent = completed[-RECENT_TURNS:]
    raw = [message for _, messages in recent for message in messages]
    original = next((m for _, messages in completed for m in messages if m["role"] == "user"), None)
    if original and (not recent or original not in recent[0][1]):
        raw.insert(0, original)
    # 为最新最终回答预留一半预算，其他消息由近到远分配。
    final_index = max(i for i, m in enumerate(raw) if m["role"] == "assistant")
    final_text = _clip(raw[final_index]["content"], MAX_CONTEXT_CHARS // 2)
    budget = MAX_CONTEXT_CHARS - len(final_text)
    selected = {final_index: {"role": "assistant", "content": final_text}}
    if original and raw[0] == original and final_index != 0:
        content = _clip(original["content"], min(2500, budget))
        selected[0] = {"role": "user", "content": content}
        budget -= len(content)
    for i in reversed(range(len(raw))):
        if i in selected or budget < 100:
            continue
        content = _clip(raw[i]["content"], min(2500, budget))
        selected[i] = {"role": raw[i]["role"], "content": content}
        budget -= len(content)
    messages = [selected[i] for i in sorted(selected)]
    signature = {"thread_id": thread["id"], "turn_id": latest["id"], "messages": raw}
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"thread_id": thread["id"], "turn_id": latest["id"], "fingerprint": fingerprint,
            "messages": messages, "title": thread.get("name") or "当前对话"}
