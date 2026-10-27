# -*- coding: utf-8 -*-
"""视频工作台：FastAPI 入口。

页面：/ 创作 | /tasks 任务列表 | /tasks/{id} 进度·预览·发布
API：任务 CRUD / 制作 / 发布 / 登录 / SSE 进度 / 视频流式
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import pipeline
import publisher
import storyboard

ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "data" / "uploads"
MAX_UPLOAD = 2 * 1024 * 1024 * 1024  # 2GB
ALLOWED_EXTS = {".mp4", ".mov"}
TITLE_MAX = 30

app = FastAPI(title="视频工作台")
templates = Jinja2Templates(directory=str(ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.on_event("startup")
def _recover() -> None:
    """服务重启：进行中的任务标记为「已中断」，可从失败步骤重试。"""
    for t in db.list_tasks():
        if t["status"] == "making":
            db.update_task(t["id"], status="interrupted", error="服务重启导致任务中断，可从失败步骤重试")


# ------------------------------------------------------------------ 页面
@app.get("/", response_class=HTMLResponse)
async def page_create(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/tasks", response_class=HTMLResponse)
async def page_tasks(request: Request):
    return templates.TemplateResponse(request, "tasks.html")


@app.get("/tasks/{tid}", response_class=HTMLResponse)
async def page_task(request: Request, tid: str):
    if not db.get_task(tid):
        raise HTTPException(404, "任务不存在")
    return templates.TemplateResponse(request, "task.html", {"tid": tid})


# ------------------------------------------------------------------ 健康检查
@app.get("/api/health")
async def health():
    ref = pipeline.ref_image()
    return {
        "dashscope_key": bool(storyboard.load_key()),
        "model": storyboard.model_name(),
        "making": bool(db.get_making_task()),
        "publish_ready": publisher.PUBLISH_SCRIPT.exists(),
        "ref_image": str(ref) if ref else "",
    }


# ------------------------------------------------------------------ 任务
@app.post("/api/tasks")
async def create_task(request: Request):
    body = await request.json()
    topic = str(body.get("topic") or "").strip()
    note = str(body.get("note") or "").strip()
    if not topic:
        raise HTTPException(400, "主题不能为空")
    if len(topic) > 200:
        raise HTTPException(400, f"主题超长（{len(topic)}/200 字）")
    if db.get_making_task():
        raise HTTPException(409, "已有制作任务进行中，同一时刻仅允许 1 个")
    tid = db.create_task("gen", topic, note)
    if not pipeline.start(tid):
        raise HTTPException(409, "已有制作任务进行中，同一时刻仅允许 1 个")
    return {"id": tid}


@app.get("/api/tasks")
async def list_tasks():
    return db.list_tasks()


@app.get("/api/tasks/{tid}")
async def get_task(tid: str):
    task = db.get_task(tid)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {
        "task": task,
        "steps": db.get_steps(tid),
        "records": db.get_publish_records(tid),
    }


@app.post("/api/tasks/{tid}/retry")
async def retry_task(tid: str):
    task = db.get_task(tid)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["type"] != "gen":
        raise HTTPException(400, "上传任务无需制作")
    if task["status"] == "making":
        raise HTTPException(409, "任务正在制作中")
    if db.get_making_task():
        raise HTTPException(409, "已有其他制作任务进行中")
    if not pipeline.start(tid):
        raise HTTPException(409, "已有制作任务进行中")
    return {"ok": True}


@app.post("/api/tasks/{tid}/publish")
async def publish_task(tid: str, request: Request):
    task = db.get_task(tid)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in ("ready", "failed"):
        raise HTTPException(409, f"当前状态（{task['status']}）不可发布")
    if not task["video_path"] or not Path(task["video_path"]).exists():
        raise HTTPException(400, "成片文件不存在")
    body = await request.json()
    title = str(body.get("title") or "").strip()
    tags = [str(t).strip().lstrip("#") for t in (body.get("tags") or []) if str(t).strip()]
    if not title:
        raise HTTPException(400, "标题不能为空")
    if len(title) > TITLE_MAX:
        raise HTTPException(400, f"标题超长（{len(title)}/{TITLE_MAX} 字符，抖音上限）")
    if not tags:
        raise HTTPException(400, "至少需要 1 个话题")
    db.update_task(tid, title=title, tags=json.dumps(tags, ensure_ascii=False))
    publisher.publish_video(tid)
    return {"ok": True}


# ------------------------------------------------------------------ 登录
@app.post("/api/login")
async def login_start():
    return {"started": publisher.start_login()}


@app.get("/api/login/status")
async def login_status():
    return publisher.login_status()


@app.post("/api/login/sms")
async def login_sms(request: Request):
    body = await request.json()
    code = str(body.get("code") or "").strip()
    if not code.isdigit():
        raise HTTPException(400, "验证码应为数字")
    publisher.submit_sms(code)
    return {"ok": True}


@app.get("/api/login/account")
async def login_account():
    return publisher.check_account()


# ------------------------------------------------------------------ 上传（F5）
@app.post("/api/upload")
async def upload_video(request: Request):
    name = urllib.parse.unquote(request.headers.get("x-file-name", ""))
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, "仅支持 mp4 / mov 格式")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    size = 0
    try:
        with open(dest, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, "文件超过 2GB 上限")
                f.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "文件为空")
    tid = db.create_task("upload", Path(name).stem[:200], "",
                         video_path=str(dest))
    db.update_task(tid, status="ready",
                   title=Path(name).stem[:TITLE_MAX], tags=json.dumps(["分享"], ensure_ascii=False))
    return {"id": tid}


# ------------------------------------------------------------------ SSE 进度
@app.get("/api/tasks/{tid}/events")
async def task_events(tid: str):
    async def gen():
        last = None
        stable = 0
        while True:
            task = db.get_task(tid)
            if not task:
                break
            snap = json.dumps({"task": task, "steps": db.get_steps(tid)},
                              ensure_ascii=False, default=str)
            if snap != last:
                last = snap
                stable = 0
                yield f"event: snapshot\ndata: {snap}\n\n"
            else:
                stable += 1
            # 终态后连续 3 次无变化即结束（客户端 EventSource 会自动关闭收尾）
            if task["status"] not in ("making", "publishing") and stable >= 3:
                break
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ 媒体
def _range_response(path: str, request: Request, mime: str):
    size = os.path.getsize(path)
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=mime)
    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
    start = int(m.group(1)) if m and m.group(1) else 0
    end = int(m.group(2)) if m and m.group(2) else size - 1
    end = min(end, size - 1)
    if start >= size:
        return JSONResponse(status_code=416, content={"detail": "range 越界"})

    def iter_file():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iter_file(), status_code=206, media_type=mime,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        })


@app.get("/api/tasks/{tid}/video")
async def task_video(tid: str, request: Request):
    task = db.get_task(tid)
    if not task or not task["video_path"] or not Path(task["video_path"]).exists():
        raise HTTPException(404, "成片不存在")
    ext = Path(task["video_path"]).suffix.lower()
    mime = "video/quicktime" if ext == ".mov" else "video/mp4"
    return _range_response(task["video_path"], request, mime)


@app.get("/api/tasks/{tid}/images/{sid}.png")
async def task_image(tid: str, sid: str):
    p = pipeline.task_dir(tid) / "images" / f"{sid}.png"
    if not p.exists():
        raise HTTPException(404, "图片不存在")
    return FileResponse(p, media_type="image/png")


@app.get("/api/tasks/{tid}/episode.json")
async def task_episode(tid: str):
    p = pipeline.task_dir(tid) / "episode.json"
    if not p.exists():
        raise HTTPException(404, "分镜文件不存在")
    return FileResponse(p, media_type="application/json")
