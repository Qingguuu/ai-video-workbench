# -*- coding: utf-8 -*-
"""制作编排器：subprocess 串联 imagegen 四脚本 + 步骤状态落库 + 失败续跑 + 单任务并发锁。

imagegen 脚本以自身目录为 ROOT 解析路径，统一 cwd=imagegen 调用、传绝对路径参数。
generate.py 批量模式部分失败仍退出码 0，故 image 步骤后必须逐一校验图片文件存在。
"""
from __future__ import annotations

import collections
import json
import subprocess
import sys
import threading
from pathlib import Path

import db
import storyboard

STUDIO_ROOT = Path(__file__).resolve().parent
IMAGEGEN_ROOT = STUDIO_ROOT.parent / "imagegen"

# 人设基准图（I2I 锁画风）：可用 studio/.env 的 REF_IMAGE 覆盖，留空则纯文生图
DEFAULT_REF_IMAGE = STUDIO_ROOT.parent / "青瓜瓜" / "人设参考" / "_preview" / "基准图_定稿_单卷须无叶_preview.jpg"


def ref_image() -> Path | None:
    import storyboard
    configured = storyboard._read_env_file(STUDIO_ROOT / ".env").get("REF_IMAGE")
    p = Path(configured) if configured else DEFAULT_REF_IMAGE
    return p if p.exists() else None

# 各步骤超时（秒）：生图最慢（每张 10-90s×N 张），合成次之
STEP_TIMEOUTS = {"storyboard": 600, "image": 2400, "voice": 900, "subtitle": 300, "video": 3600}

_run_lock = threading.Lock()   # 串行化「并发检查 + 置 making」
_running = set()               # 正在执行的 task_id（防同一任务重复起线程）


def task_dir(tid: str) -> Path:
    return STUDIO_ROOT / "data" / "tasks" / tid


def start(tid: str) -> bool:
    """尝试启动/续跑任务；同一时刻仅 1 个制作任务（排除自身，因为
    create_task 插入时即为 making，不排除会查到自己导致永远无法启动）。"""
    with _run_lock:
        others = [t for t in db.list_tasks() if t["status"] == "making" and t["id"] != tid]
        if others:
            return False
        db.update_task(tid, status="making", error="")
        _running.add(tid)
    threading.Thread(target=_run, args=(tid,), daemon=True).start()
    return True


def _run(tid: str) -> None:
    try:
        task = db.get_task(tid)
        if not task or task["type"] != "gen":
            return
        episode = None
        for step in db.GEN_STEPS:
            if db.step_status(tid, step) == "done":
                continue
            db.update_step(tid, step, "running", reset_time=True)
            try:
                if step == "storyboard":
                    episode = _step_storyboard(tid, task)
                elif step == "image":
                    _step_image(tid, episode or _load_episode(tid))
                elif step == "voice":
                    _step_voice(tid, episode or _load_episode(tid))
                elif step == "subtitle":
                    _step_subtitle(tid, episode or _load_episode(tid))
                elif step == "video":
                    _step_video(tid, episode or _load_episode(tid))
                db.update_step(tid, step, "done", detail=_last_detail.get(step, ""))
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
                db.update_step(tid, step, "failed", error=msg[:800])
                db.update_task(tid, status="failed", error=msg[:800])
                return
        episode = episode or _load_episode(tid)
        db.update_task(tid, status="ready",
                       title=episode["publish"]["title"],
                       tags=json.dumps(episode["publish"]["tags"], ensure_ascii=False))
    finally:
        with _run_lock:
            _running.discard(tid)


def _load_episode(tid: str) -> dict:
    p = task_dir(tid) / "episode.json"
    if not p.exists():
        raise RuntimeError("episode.json 不存在（分镜步骤未完成？）")
    return json.loads(p.read_text(encoding="utf-8"))


# 步骤 detail 便于前端展示最后几行脚本输出
_last_detail: dict[str, str] = {}


def _run_script(args: list[str], step: str, timeout: int) -> str:
    """cwd=imagegen 跑脚本，收集尾部输出；超时/非零退出抛异常。"""
    cmd = [sys.executable, *args]
    proc = subprocess.Popen(
        cmd, cwd=str(IMAGEGEN_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    tail: collections.deque[str] = collections.deque(maxlen=15)

    def drain():
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                tail.append(line)

    th = threading.Thread(target=drain, daemon=True)
    th.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(f"步骤超时（>{timeout}s），已终止")
    th.join(timeout=5)
    detail = "\n".join(list(tail)[-8:])
    if proc.returncode != 0:
        raise RuntimeError(f"脚本退出码 {proc.returncode}：{detail[-500:]}")
    _last_detail[step] = detail
    return detail


def _step_storyboard(tid: str, task: dict) -> dict:
    episode = storyboard.generate(task["topic"], task["note"], tid, task_dir(tid))
    db.update_task(tid, episode_json=json.dumps(episode, ensure_ascii=False))
    _last_detail["storyboard"] = f"{len(episode['scenes'])} 个分镜，标题：{episode['publish']['title']}"
    return episode


def _step_image(tid: str, episode: dict) -> None:
    plan = json.loads((task_dir(tid) / "scenes_plan.json").read_text(encoding="utf-8"))
    out_dir = Path(plan["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    # 续跑/重试只生成缺失的图片（generate.py 本身有跳过缓存，这里再过滤一次减少无效请求）
    todo = [s for s in plan["scenes"] if not (out_dir / f"{s['id']}.png").exists()]
    if not todo:
        _last_detail["image"] = f"全部 {len(plan['scenes'])} 张图片已存在，跳过"
        return
    todo_path = task_dir(tid) / "scenes_todo.json"
    plan_todo: dict = {"output_dir": plan["output_dir"], "scenes": todo}
    ref = ref_image()
    if ref:
        # I2I 基准图锁画风（generate.py 全局 ref_images，最多 3 张，路径须存在）
        plan_todo["ref_images"] = [str(ref)]
        _last_detail["image"] = f"基准图：{ref.name}"
    todo_path.write_text(
        json.dumps(plan_todo, ensure_ascii=False), encoding="utf-8")
    _run_script(["generate.py", "--file", str(todo_path)], "image", STEP_TIMEOUTS["image"])
    missing = [s["id"] for s in todo if not (out_dir / f"{s['id']}.png").exists()]
    if missing:
        raise RuntimeError(f"生图未完成，缺失：{', '.join(missing)}（重试仅补缺失分镜）")


def _step_voice(tid: str, episode: dict) -> None:
    _run_script(["voice.py", "--episode", str(task_dir(tid) / "episode.json")],
                "voice", STEP_TIMEOUTS["voice"])


def _step_subtitle(tid: str, episode: dict) -> None:
    _run_script(["subtitle.py", "--episode", str(task_dir(tid) / "episode.json")],
                "subtitle", STEP_TIMEOUTS["subtitle"])


def _step_video(tid: str, episode: dict) -> None:
    _run_script(["video.py", "--episode", str(task_dir(tid) / "episode.json")],
                "video", STEP_TIMEOUTS["video"])
    final = IMAGEGEN_ROOT / "build" / tid / f"{episode['title']}.mp4"
    if not final.exists():
        raise RuntimeError(f"成片未产出：{final}")
    db.update_task(tid, video_path=str(final))
