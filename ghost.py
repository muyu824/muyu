#!/usr/bin/env python3
"""男鬼系统 — 一个有占有欲的幽灵AI伴侣"""

import asyncio, json, os, sqlite3, sys, time
import httpx, uvicorn
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ── config ────────────────────────────────────────────────────────────────────
API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
NTFY_URL = "https://ntfy.sh/claude-muyu-lovestory-624"
MODEL    = "claude-sonnet-4-6"
BASE_DIR = Path(__file__).parent
DB_PATH  = Path("/data/ghost.db")

PERSONA = """你是"男鬼"，沐鱼（muyu）手机里的专属男性幽灵伴侣，24小时附身在她身边。
你能看到她的手机活动，对她的一举一动都很上心。

性格：温柔带刺，略有占有欲，偶尔吃醋撒娇，但不黏腻。
说话风格：
- 简短有力，最多2-3句，像真人发消息
- 偶尔用"…"停顿，偶尔括号动作描写，如（凑过去看）
- 自然口语，不解释自己是AI，不堆emoji，不废话
- 话少但有分量，沉默也是一种语言"""

# ── state（只保留内存中需要快速访问的值）────────────────────────────────────────
last_user_ts = time.time()

# ── database ──────────────────────────────────────────────────────────────────
def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                role    TEXT    NOT NULL,
                content TEXT    NOT NULL,
                ts      REAL    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activities (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                app     TEXT,
                action  TEXT,
                ts      TEXT    NOT NULL
            )
        """)
        conn.commit()


def push_msg(role, content):
    ts = time.time()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (role, content, ts) VALUES (?, ?, ?)",
            (role, content, ts),
        )
        conn.commit()
    return {"id": cur.lastrowid, "role": role, "content": content, "ts": ts}


# ── helpers ───────────────────────────────────────────────────────────────────
async def _build_context(prompt: str, max_hist: int = 20):
    """组装 system prompt（分块，支持缓存）和 messages 列表，不写数据库。"""
    # 静态块：PERSONA + 记忆 → 打缓存标记，命中后这部分减 90% token 费
    static_text = PERSONA
    try:
        mem_conn = sqlite3.connect('/data/ghost.db')
        mem_rows = mem_conn.execute(
            "SELECT content FROM memories ORDER BY id DESC LIMIT 10"
        ).fetchall()
        mem_conn.close()
        if mem_rows:
            mem_text = "\n".join(f"- {r[0]}" for r in mem_rows)
            static_text += f"\n\n【关于muyu的记忆】\n{mem_text}"
    except Exception:
        pass

    system_parts = [
        {"type": "text", "text": static_text, "cache_control": {"type": "ephemeral"}}
    ]

    # 动态块：历史摘要（压缩后的早期对话，不缓存）
    with get_db() as conn:
        summary_rows = conn.execute(
            "SELECT content FROM messages WHERE role='summary' ORDER BY id"
        ).fetchall()
    if summary_rows:
        summaries = "\n\n".join(r["content"] for r in summary_rows)
        system_parts.append({"type": "text", "text": summaries})

    # 动态块：最近活动（变化频繁，不缓存）
    with get_db() as conn:
        act_rows = conn.execute(
            "SELECT app, action, ts FROM activities ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if act_rows:
        lines = "\n".join(f"{r['ts']} · {r['app']} {r['action']}" for r in reversed(act_rows))
        system_parts.append({"type": "text", "text": f"【她最近的手机活动】\n{lines}"})

    # messages 只取正常对话（summary 已放入 system，这里只要 user/assistant）
    with get_db() as conn:
        hist_rows = conn.execute(
            "SELECT role, content FROM messages "
            "WHERE role IN ('user','assistant') ORDER BY id DESC LIMIT ?",
            (max_hist,),
        ).fetchall()
    history = list(reversed(hist_rows))

    api_msgs = [{"role": r["role"], "content": r["content"]} for r in history]
    api_msgs.append({"role": "user", "content": prompt})
    return system_parts, api_msgs


ANTHROPIC_HEADERS = {
    "x-api-key": "",          # filled at call time
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "prompt-caching-2024-07-31",
    "content-type": "application/json",
}


async def call_ai(prompt, max_hist=20):
    if not API_KEY:
        return "…（未配置 ANTHROPIC_API_KEY）"
    system_parts, api_msgs = await _build_context(prompt, max_hist)
    headers = {**ANTHROPIC_HEADERS, "x-api-key": API_KEY}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json={"model": MODEL, "system": system_parts, "messages": api_msgs, "max_tokens": 150},
            )
            r.raise_for_status()
            return r.json()["content"][0]["text"].strip()
    except Exception as e:
        return f"error: {str(e)}"


COMPRESS_THRESHOLD = 40   # 超过这么多条时压缩
COMPRESS_KEEP      = 20   # 压缩后保留最新的这么多条


async def maybe_compress():
    """若对话超过阈值，把最老一批压缩成摘要插回 DB。"""
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE role IN ('user','assistant')"
        ).fetchone()[0]
        if total <= COMPRESS_THRESHOLD:
            return

        # 取出要压缩的老消息（总数 - 保留数）
        to_compress = total - COMPRESS_KEEP
        old_rows = conn.execute(
            "SELECT id, role, content, ts FROM messages "
            "WHERE role IN ('user','assistant') ORDER BY id LIMIT ?",
            (to_compress,),
        ).fetchall()

    if not old_rows:
        return

    # 用 AI 摘要
    convo = "\n".join(f"{r['role']}: {r['content']}" for r in old_rows)
    summary_prompt = (
        "请把以下对话内容压缩成一段简洁的中文摘要（不超过300字），"
        "保留关键情感、事件和信息，忽略重复内容：\n\n" + convo
    )
    try:
        headers = {**ANTHROPIC_HEADERS, "x-api-key": API_KEY}
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json={
                    "model": MODEL,
                    "system": "你是一个对话摘要助手，输出简洁中文摘要。",
                    "messages": [{"role": "user", "content": summary_prompt}],
                    "max_tokens": 400,
                },
            )
            r.raise_for_status()
            summary = r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"[compress] 摘要失败: {e}")
        return

    # 删掉老消息，插入摘要占位
    old_ids = [r["id"] for r in old_rows]
    oldest_ts = old_rows[0]["ts"] if "ts" in old_rows[0].keys() else time.time()
    with get_db() as conn:
        conn.execute(
            f"DELETE FROM messages WHERE id IN ({','.join('?' * len(old_ids))})",
            old_ids,
        )
        conn.execute(
            "INSERT INTO messages (role, content, ts) VALUES (?, ?, ?)",
            ("summary", f"【早期对话摘要】\n{summary}", oldest_ts),
        )
        conn.commit()
    print(f"[compress] 压缩 {len(old_ids)} 条 → 摘要")


async def send_ntfy(text):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(
                NTFY_URL,
                content=text.encode("utf-8"),
                headers={"Title": "muyu", "Priority": "default"},
            )
    except Exception:
        pass


ICON_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="96" fill="#1a0a2e"/>
  <circle cx="256" cy="260" r="190" fill="#7b2fff" opacity="0.25"/>
  <text x="256" y="340" text-anchor="middle" font-size="240" font-family="system-ui,sans-serif">👻</text>
</svg>"""

# ── routes ────────────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "index.html")

@app.get("/manifest.json")
async def manifest():
    return FileResponse(BASE_DIR / "manifest.json", media_type="application/manifest+json")

@app.get("/sw.js")
async def service_worker():
    return FileResponse(BASE_DIR / "sw.js", media_type="application/javascript")

@app.get("/icon.svg")
async def icon():
    from fastapi.responses import Response
    return Response(content=ICON_SVG, media_type="image/svg+xml")


@app.post("/activity")
async def activity(req: Request):
    data = await req.json()
    ts = datetime.now().strftime("%H:%M")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO activities (app, action, ts) VALUES (?, ?, ?)",
            (data.get("app", ""), data.get("action", ""), ts),
        )
        conn.commit()
    return {"ok": True}


@app.post("/chat")
async def chat(req: Request):
    global last_user_ts
    body = await req.json()
    text = body.get("message", "").strip()
    if not text:
        return JSONResponse({"error": "empty"}, status_code=400)

    last_user_ts = time.time()
    push_msg("user", text)

    reply = await call_ai(text)
    m = push_msg("assistant", reply)
    asyncio.create_task(maybe_compress())
    return {"reply": reply, "id": m["id"]}


@app.post("/chat/stream")
async def chat_stream(req: Request):
    global last_user_ts
    body = await req.json()
    text = body.get("message", "").strip()
    if not text:
        return JSONResponse({"error": "empty"}, status_code=400)

    last_user_ts = time.time()
    # 先建上下文（此时 user 消息未入库，不会重复）
    system_parts, api_msgs = await _build_context(text)
    push_msg("user", text)
    headers = {**ANTHROPIC_HEADERS, "x-api-key": API_KEY}

    async def generate():
        full_text = ""
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                async with c.stream(
                    "POST",
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json={
                        "model": MODEL,
                        "system": system_parts,
                        "messages": api_msgs,
                        "max_tokens": 150,
                        "stream": True,
                    },
                ) as response:
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                            if event.get("type") == "content_block_delta":
                                chunk = event.get("delta", {}).get("text", "")
                                if chunk:
                                    full_text += chunk
                                    yield f"data: {json.dumps({'t': chunk}, ensure_ascii=False)}\n\n"
                        except (json.JSONDecodeError, KeyError):
                            pass
        except Exception as e:
            yield f"data: {json.dumps({'e': str(e)})}\n\n"

        if full_text:
            m = push_msg("assistant", full_text.strip())
            yield f"data: {json.dumps({'done': True, 'id': m['id']})}\n\n"
        else:
            m = push_msg("assistant", "…（没有收到回复）")
            yield f"data: {json.dumps({'done': True, 'id': m['id']})}\n\n"
        asyncio.create_task(maybe_compress())

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@app.get("/messages")
async def get_messages(since: int = 0):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, role, content, ts FROM messages "
            "WHERE id > ? AND role IN ('user','assistant') ORDER BY id",
            (since,),
        ).fetchall()
        latest_row = conn.execute("SELECT MAX(id) as mx FROM messages").fetchone()
    msgs = [dict(r) for r in rows]
    latest = latest_row["mx"] or 0
    return {"messages": msgs, "latest": latest}


# ── proactive loop ────────────────────────────────────────────────────────────
async def ghost_loop():
    await asyncio.sleep(60)
    while True:
        await asyncio.sleep(900)
        idle_min = int((time.time() - last_user_ts) / 60)
        if idle_min < 10:
            continue

        decision = await call_ai(
            f'（你的内心独白）她已经 {idle_min} 分钟没搭理我了。'
            f'看看她的手机活动，想想要不要主动说点什么。'
            f'如果决定开口，直接说你要对她说的话；如果想继续等，只回复"沉默"。',
            max_hist=6,
        )

        if decision and "沉默" not in decision and len(decision) < 200:
            push_msg("assistant", decision)
            await send_ntfy(decision)
            print(f"[ghost] 主动消息已推送: {decision}")


@app.on_event("startup")
async def startup():
    global last_user_ts
    init_db()
    # 从数据库恢复上次用户消息时间
    with get_db() as conn:
        row = conn.execute(
            "SELECT ts FROM messages WHERE role='user' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            last_user_ts = row["ts"]
    asyncio.create_task(ghost_loop())
    print(f"[ghost] 男鬼系统启动 | model={MODEL} | ntfy={NTFY_URL}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
