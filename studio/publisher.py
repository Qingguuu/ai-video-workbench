# -*- coding: utf-8 -*-
"""发布封装：调用 publish/douyin_publish.py（Playwright + 持久化登录态），结果落库。

登录协议见 publish/login_state.py：状态 JSON 文件（starting → qr_ready/window_login →
sms_required → success | error），短信验证码通过 <qr目录>/douyin.code 文件回填。
同一时刻仅 1 个发布进程（Chromium profile 目录不能被两个进程同时使用）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import db

STUDIO_ROOT = Path(__file__).resolve().parent
PUBLISH_SCRIPT = STUDIO_ROOT / "publish" / "douyin_publish.py"
DATA_DIR = STUDIO_ROOT / "data"
LOGIN_STATUS = DATA_DIR / "login_status.json"
LOGIN_CODE = DATA_DIR / "douyin.code"

_publish_lock = threading.Lock()
_login_running = False


def _tail(proc_text: str, n: int = 6) -> str:
    lines = [l for l in (proc_text or "").splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def check_account() -> dict:
    """同步跑 whoami 校验登录态（起一次无头浏览器，约 5~15s）。"""
    try:
        r = subprocess.run(
            [sys.executable, str(PUBLISH_SCRIPT), "whoami"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        for line in reversed((r.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return {"loggedIn": False, "name": "", "avatar": "", "error": (r.stderr or r.stdout or "")[-300:]}
    except Exception as e:  # noqa: BLE001
        return {"loggedIn": False, "name": "", "avatar": "", "error": f"{type(e).__name__}: {e}"}


def start_login() -> bool:
    """后台弹有头浏览器扫码登录（含可能的短信验证）。"""
    global _login_running
    with _publish_lock:
        if _login_running:
            return False
        _login_running = True
    threading.Thread(target=_run_login, daemon=True).start()
    return True


def _run_login() -> None:
    global _login_running
    try:
        subprocess.run(
            [sys.executable, str(PUBLISH_SCRIPT), "login", "--headed",
             "--timeout", "600", "--status-file", str(LOGIN_STATUS),
             "--sms-code-file", str(LOGIN_CODE)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=700,
        )
    except Exception as e:  # noqa: BLE001
        import login_state
        login_state.write_status(str(LOGIN_STATUS), "error", f"{type(e).__name__}: {e}")
    finally:
        _login_running = False


def login_status() -> dict:
    try:
        import login_state
        return login_state.read_status(str(LOGIN_STATUS))
    except Exception as e:  # noqa: BLE001
        return {"state": "unknown", "message": f"{type(e).__name__}: {e}", "qr": "", "ts": 0}


def submit_sms(code: str) -> None:
    LOGIN_CODE.parent.mkdir(parents=True, exist_ok=True)
    LOGIN_CODE.write_text(code.strip(), encoding="utf-8")


def publish_video(tid: str) -> None:
    """后台线程执行发布：读任务 → 调 publish-video --exec → 结果落库。"""
    threading.Thread(target=_run_publish, args=(tid,), daemon=True).start()


def _run_publish(tid: str) -> None:
    with _publish_lock:
        task = db.get_task(tid)
        if not task or task["status"] == "publishing":
            return
        if not Path(task["video_path"]).exists():
            db.update_task(tid, status="failed", error="成片文件不存在，无法发布")
            return
        db.update_task(tid, status="publishing", error="")
        tags = db.tags_of(task)
        cmd = [
            sys.executable, str(PUBLISH_SCRIPT), "publish-video",
            "--video", task["video_path"],
            "--title", task["title"],
            "--tags", ",".join(tags),
            "--exec", "--status-file", str(DATA_DIR / "publish_status.json"),
        ]
        detail = ""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=1800)
            out, err = r.stdout or "", r.stderr or ""
            detail = _tail(out) + ("\n" + _tail(err) if err.strip() else "")
            if r.returncode == 0:
                db.add_publish_record(tid, task["video_path"], task["title"], tags,
                                      "success", detail)
                db.update_task(tid, status="published", error="")
            else:
                if "未登录" in (out + err):
                    err_msg = "抖音登录态已失效，请先扫码登录再发布"
                elif r.returncode == 4:
                    err_msg = "短信验证未完成，请重新发布并完成验证"
                else:
                    err_msg = f"发布失败（退出码 {r.returncode}）：{detail[-400:]}"
                db.add_publish_record(tid, task["video_path"], task["title"], tags,
                                      "failed", err_msg)
                db.update_task(tid, status="ready", error=err_msg)
        except Exception as e:  # noqa: BLE001
            err_msg = f"{type(e).__name__}: {e}"
            detail = err_msg
            db.add_publish_record(tid, task["video_path"], task["title"], tags,
                                  "failed", err_msg)
            db.update_task(tid, status="ready", error=err_msg)
