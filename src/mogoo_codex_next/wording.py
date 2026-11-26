"""Generation instructions and matching preview/final presentation limits."""
from __future__ import annotations

TITLE_LIMIT = 16
REASON_LIMIT = 50


def wording_policy(config: dict) -> str:
    if config.get("output_mode", "concise") == "standard":
        return ""
    rules = []
    rules.append(f"title 用短动宾短语，尽量 4–10 个中文字符，最多 {TITLE_LIMIT} 个字符（含标点）；外文也遵守字符上限。")
    rules.append(f"reason 只写一句话，尽量 15–25 个中文字符，最多 {REASON_LIMIT} 个字符（含标点）；不列举、不换行、不追加第二句。")
    rules.append("直接生成符合上述长度的文字，不要先写长文再解释；prompt 保留执行所需的目标、范围和限制，不为缩短标题或理由而删减指令。")
    rules.append("prompt 尽量写在 80 个字符左右（含标点）；保留目标、范围、关键条件和必要的验收要求。复杂任务可超出目标长度，不为凑字数删减条件或改变原意。")
    return "\n".join(rules)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _first_sentence(text: str) -> str:
    # A period within a version, decimal or URL is not a sentence boundary.
    # Quoted punctuation belongs to the surrounding sentence.
    closers = []
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』", "（": "）", "(": ")"}
    for index, char in enumerate(text):
        if char in pairs:
            closers.append(pairs[char])
        elif closers and char == closers[-1]:
            closers.pop()
        elif not closers and (char in "。！？!?" or (char == "." and (index + 1 == len(text) or text[index + 1].isspace()))):
            return text[:index + 1]
    return text


def format_suggestions(suggestions: list[dict], config: dict) -> list[dict]:
    """Apply only to validated metadata; never shorten the editable prompt."""
    result = []
    for item in suggestions:
        item = dict(item)
        if config.get("output_mode", "concise") == "concise":
            item["title"] = _clip(" ".join(item["title"].split()), TITLE_LIMIT)
            text = " ".join(item["reason"].split())
            item["reason"] = _clip(_first_sentence(text), REASON_LIMIT)
        result.append(item)
    return result
