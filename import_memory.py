#!/usr/bin/env python3
"""
import_memory.py
读取 conversations.json，提取关于 muyu 的个人信息摘要，存入 /data/ghost.db
"""

import json, os, sqlite3, sys, time
from datetime import datetime
from pathlib import Path

import httpx

# ── config ────────────────────────────────────────────────────────────────────
API_KEY       = os.environ.get("OPENROUTER_API_KEY", "")
MODEL         = "meta-llama/llama-3.3-70b-instruct"
DB_PATH       = Path("./ghost_memories.db")
CONV_FILE     = Path("conversations.json")
DELAY_BETWEEN = 1.5   # 每次API调用之间的间隔（秒），避免触发限速

EXTRACT_PROMPT = """以下是一段对话记录。请从中提取所有关于用户（muyu/沐鱼）的个人信息，包括：
- 个人信息（年龄、职业、所在地等）
- 兴趣爱好、喜欢/不喜欢的事物
- 人生经历、重要事件、情感状态
- 习惯、价值观、性格特点
- 提到的人际关系

如果这段对话纯粹是技术讨论（写代码、修bug等），和muyu个人无关，只回复"SKIP"。
否则，用中文写一段简洁的摘要，只包含有价值的个人信息，不超过300字。

对话内容：
{chat_text}
"""

# ── database ──────────────────────────────────────────────────────────────────
def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_memories_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            source     TEXT    NOT NULL,
            content    TEXT    NOT NULL,
            created_at TEXT    NOT NULL
        )
    """)
    conn.commit()


def already_imported(conn, source):
    row = conn.execute(
        "SELECT id FROM memories WHERE source = ?", (source,)
    ).fetchone()
    return row is not None


def save_memory(conn, source, content):
    conn.execute(
        "INSERT INTO memories (source, content, created_at) VALUES (?, ?, ?)",
        (source, content, datetime.now().isoformat()),
    )
    conn.commit()


# ── api ───────────────────────────────────────────────────────────────────────
def summarize(chat_text: str) -> str | None:
    """调用 OpenRouter，返回摘要；纯技术对话返回 None。"""
    prompt = EXTRACT_PROMPT.format(chat_text=chat_text[:6000])  # 截断超长对话

    resp = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "X-Title": "ghost-memory"},
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500,
        },
        timeout=60,
    )
    resp.raise_for_status()
    result = resp.json()["choices"][0]["message"]["content"].strip()
    return None if result.upper().startswith("SKIP") else result


# ── main ──────────────────────────────────────────────────────────────────────
def build_chat_text(chat_messages: list) -> str:
    lines = []
    for msg in chat_messages:
        sender = msg.get("sender", "unknown")
        text   = msg.get("text", "").strip()
        if text:
            lines.append(f"{sender}: {text}")
    return "\n".join(lines)


def main():
    if not API_KEY:
        sys.exit("错误：未设置 OPENROUTER_API_KEY 环境变量")

    if not CONV_FILE.exists():
        sys.exit(f"错误：找不到 {CONV_FILE}，请把文件放在当前目录")

    print(f"读取 {CONV_FILE} …")
    conversations = json.loads(CONV_FILE.read_text("utf-8"))
    total = len(conversations)
    print(f"共 {total} 段对话\n")

    conn = get_db()
    init_memories_table(conn)

    skipped = imported = errors = already = 0

    for i, conv in enumerate(conversations, 1):
        name = conv.get("name") or f"conversation_{i}"
        msgs = conv.get("chat_messages", [])

        print(f"[{i}/{total}] {name[:60]}", end=" … ", flush=True)

        if not msgs:
            print("空对话，跳过")
            skipped += 1
            continue

        if already_imported(conn, name):
            print("已导入，跳过")
            already += 1
            continue

        chat_text = build_chat_text(msgs)
        if not chat_text.strip():
            print("无文本，跳过")
            skipped += 1
            continue

        try:
            summary = summarize(chat_text)
            if summary is None:
                print("技术对话，跳过")
                skipped += 1
            else:
                save_memory(conn, name, summary)
                print(f"已存入（{len(summary)} 字）")
                imported += 1
        except Exception as e:
            print(f"失败：{e}")
            errors += 1

        time.sleep(DELAY_BETWEEN)

    conn.close()
    print(f"\n完成：导入 {imported} 条，跳过 {skipped} 条，已有 {already} 条，失败 {errors} 条")
    print(f"数据库：{DB_PATH}")


if __name__ == "__main__":
    main()
