"""XM05 运维知识库检索（BM25）。

参考 XM03 retriever.py 的 BM25 写法（复制不引用，不改 XM03）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jieba
from rank_bm25 import BM25Okapi

KB_PATH = Path(__file__).resolve().parent / "kb.json"

STOP_WORDS = {
    "的", "了", "是", "在", "和", "有", "为", "与", "及", "什么", "哪些",
    "我", "你", "他", "她", "它", "这", "那", "就", "都", "而", "或",
    "被", "把", "对", "从", "到", "中", "上", "下", "里", "后", "前",
    "吗", "呢", "啊", "可以", "进行", "以及", "这个", "那个", "一种",
    "其中", "通过", "如何", "怎么", "为什么", "出现", "发生", "问题",
    "。", "，", "；", "：", "？", "！", "、", ".", ",", ";", ":", "?", "!",
}

_kb: list[dict] = []
_bm25: BM25Okapi | None = None
_index_ready = False


def _tokenize(text: str) -> list[str]:
    tokens = []
    for token in jieba.lcut(text):
        normalized = token.strip().lower()
        if normalized and normalized not in STOP_WORDS:
            tokens.append(normalized)
    return tokens


def _kb_text(entry: dict) -> str:
    """把条目拼成可检索文本：类别 + 现象 + 原因 + 处置。"""
    parts = [
        str(entry.get("category") or ""),
        str(entry.get("symptom") or ""),
        " ".join(entry.get("causes") or []),
        " ".join(entry.get("actions") or []),
    ]
    return " ".join(parts)


def build_index() -> int:
    global _kb, _bm25, _index_ready
    if not KB_PATH.is_file():
        raise FileNotFoundError(f"知识库文件不存在: {KB_PATH}")
    with KB_PATH.open("r", encoding="utf-8") as f:
        kb = json.load(f)
    if not isinstance(kb, list):
        raise ValueError("知识库应为 JSON 数组")
    tokenized = [_tokenize(_kb_text(e)) for e in kb]
    _kb = kb
    _bm25 = BM25Okapi(tokenized) if tokenized else None
    _index_ready = True
    return len(kb)


def search(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """检索知识库，返回 [{entry, score}, ...]，无匹配返回空列表（不报错）。"""
    if top_k <= 0:
        return []
    if not _index_ready:
        build_index()
    if _bm25 is None:
        return []
    query_tokens = _tokenize(query if isinstance(query, str) else str(query))
    if not query_tokens:
        return []
    scores = _bm25.get_scores(query_tokens)
    ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    # 只保留 score > 0 的（有实际匹配）
    return [
        {"entry": _kb[i], "score": round(float(s), 4)}
        for i, s in ranked
        if s > 0
    ][:top_k]
