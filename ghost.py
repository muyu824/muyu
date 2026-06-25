#!/usr/bin/env python3
"""男鬼系统 — 一个有占有欲的幽灵AI伴侣"""

import asyncio, os, sqlite3, sys, time
import httpx, uvicorn
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ── config ────────────────────────────────────────────────────────────────────
API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
NTFY_URL = "https://ntfy.sh/claude-muyu-lovestory-624"
MODEL    = "meta-llama/llama-3.3-70b-instruct"
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
async def call_ai(prompt, max_hist=20):
    if not API_KEY:
        return "…（未配置 OPENROUTER_API_KEY）"

    system = PERSONA

    try:
        import sqlite3
        mem_conn = sqlite3.connect('/data/ghost.db')
        mem_rows = mem_conn.execute(
            "SELECT content FROM memories ORDER BY id DESC LIMIT 10"
        ).fetchall()
        mem_conn.close()
        if mem_rows:
            mem_text = "\n".join(f"- {r[0]}" for r in mem_rows)
            system += f"\n\n【关于muyu的记忆】\n{mem_text}"
    except Exception:
        pass

    # 读取最近活动
    with get_db() as conn:
        act_rows = conn.execute(
            "SELECT app, action, ts FROM activities ORDER BY id DESC LIMIT 10"
        ).fetchall()
    if act_rows:
        lines = "\n".join(
            f"{r['ts']} · {r['app']} {r['action']}" for r in reversed(act_rows)
        )
        system += f"\n\n【她最近的手机活动】\n{lines}"

    # 读取对话历史
    with get_db() as conn:
        hist_rows = conn.execute(
            "SELECT role, content FROM messages "
            "WHERE role IN ('user','assistant') ORDER BY id DESC LIMIT ?",
            (max_hist,),
        ).fetchall()
    history = list(reversed(hist_rows))

    api_msgs = [{"role": "system", "content": system}]
    for r in history:
        api_msgs.append({"role": r["role"], "content": r["content"]})
    api_msgs.append({"role": "user", "content": prompt})

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}", "X-Title": "ghost"},
                json={"model": MODEL, "messages": api_msgs, "max_tokens": 150},
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"error: {str(e)}"


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


# ── routes ────────────────────────────────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "index.html")


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
    return {"reply": reply, "id": m["id"]}


@app.get("/messages")
async def get_messages(since: int = 0):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, role, content, ts FROM messages WHERE id > ? ORDER BY id",
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
