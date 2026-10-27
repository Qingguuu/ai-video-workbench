# -*- coding: utf-8 -*-
"""分镜生成：主题 → episode.json + scenes_plan.json。

调 DashScope OpenAI 兼容端点的文本模型（默认 qwen-plus），
结构对齐 imagegen/episodes/ep01.json（few-shot）：
  episode.json = {id, title, image_dir, publish:{title,tags,desc}, scenes:[{id,image,narration}]}
  scenes_plan.json = {output_dir, scenes:[{id,prompt}]}  （给 generate.py --file 用）
图片文件名由后端预分配（s01.png…），不写 per-scene style_anchor（generate.py 存在循环变量 bug）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

STUDIO_ROOT = Path(__file__).resolve().parent
IMAGEGEN_ROOT = STUDIO_ROOT.parent / "imagegen"
ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

SYSTEM_PROMPT = """你是短视频编导，为一条竖版火柴人动画科普/观点短视频写分镜。

角色与画风已由系统全局风格锚点固定（白色大圆头火柴人、头顶绿色小卷须、极简扁平插画风、竖版9:16），
因此画面描述 prompt 里不要描述画风，只写具体场景、动作、情绪和构图。

你的输出必须是一个纯 JSON 对象（禁止 markdown 围栏、禁止解释文字），结构：
{
  "publish": {"title": "作品标题", "tags": ["话题1", "话题2"], "desc": "一句话简介"},
  "scenes": [{"id": "s01", "image": "s01.png", "prompt": "画面描述", "narration": "口播文案"}]
}
硬性要求：
1. 6~10 个分镜，id 从 s01 起两位数递增（s01、s02…），image 恒等于 "<id>.png"。
2. 每镜 narration 是口播文案，一两句话（≤60字），全部 narration 合计 220~380 字；口语化，直接跟观众说话。
3. 每镜 prompt 用中文写画面：场景、人物动作、表情；火柴人角色特征不用重复写；禁止要求画面出现文字、字幕、水印。
4. publish.title ≤30 个字符（抖音标题上限），有钩子感；tags 3~5 个，不带 # 号；desc 一句话。
5. 内容围绕主题展开，观点清楚，有起承转合，结尾引导互动（提问/关注）。"""

FEWSHOT = """示例（主题：AI 是怎么回答你的）：
{"publish": {"title": "它从来没有回答过你任何问题", "tags": ["AI", "人工智能", "科普"], "desc": "你和 AI 说话时，到底发生了什么？"},
 "scenes": [
  {"id": "s01", "image": "s01.png", "prompt": "火柴人坐在书桌前对着电脑，屏幕发出光，桌上放着表格文件，表情疑惑", "narration": "上班时，你让 AI 整理过表格吗？"},
  {"id": "s02", "image": "s02.png", "prompt": "深夜房间，火柴人躺在床上举着手机，屏幕的光照亮脸部，窗外一片深蓝", "narration": "深夜问过它：他还爱我吗？多久没直接问 AI 了？"},
  {"id": "s03", "image": "s03.png", "prompt": "火柴人站在画面中央对观众摊开双手，头顶卷须微微晃动，背景纯色", "narration": "AI 无处不在，但它从没回答过你。"}
 ]}"""


def _read_env_file(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def load_key() -> str | None:
    """Key 优先级：环境变量 → studio/.env → imagegen/.env。"""
    if os.environ.get("DASHSCOPE_API_KEY"):
        return os.environ["DASHSCOPE_API_KEY"]
    for f in (STUDIO_ROOT / ".env", IMAGEGEN_ROOT / ".env"):
        v = _read_env_file(f).get("DASHSCOPE_API_KEY")
        if v:
            return v
    return None


def model_name() -> str:
    env = os.environ.get("STORYBOARD_MODEL") or _read_env_file(STUDIO_ROOT / ".env").get("STORYBOARD_MODEL")
    return env or "qwen-plus"


def _extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出中没有 JSON 对象")
    return json.loads(text[start:end + 1])


def _validate(data: dict) -> tuple[dict, list]:
    pub = data.get("publish") or {}
    scenes_in = data.get("scenes") or []
    title = str(pub.get("title") or "").strip()
    tags = [str(t).strip().lstrip("#") for t in (pub.get("tags") or []) if str(t).strip()]
    desc = str(pub.get("desc") or "").strip()
    if not title:
        raise ValueError("缺少 publish.title")
    if len(title) > 30:
        raise ValueError(f"title 超过 30 字符（{len(title)}）")
    if not tags:
        raise ValueError("缺少 publish.tags")
    if not scenes_in:
        raise ValueError("缺少 scenes")
    scenes, plan_scenes = [], []
    for i, s in enumerate(scenes_in, 1):
        sid = f"s{i:02d}"
        prompt = str(s.get("prompt") or "").strip()
        narration = str(s.get("narration") or "").strip()
        if not prompt or not narration:
            raise ValueError(f"第 {i} 镜缺少 prompt 或 narration")
        scenes.append({"id": sid, "image": f"{sid}.png", "narration": narration})
        plan_scenes.append({"id": sid, "prompt": prompt})
    return {"publish": {"title": title, "tags": tags[:5], "desc": desc}}, \
           [{"id": s["id"], "prompt": p["prompt"]} for s, p in zip(scenes, plan_scenes)], scenes


def generate(topic: str, note: str, task_id: str, task_dir: Path) -> dict:
    """生成并落盘 episode.json + scenes_plan.json，返回 episode dict。失败抛异常。"""
    key = load_key()
    if not key:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY（检查 imagegen/.env 或 studio/.env）")
    user = f"【主题】{topic}\n【补充说明】{note or '无'}\n\n参考结构与文风（不要照抄内容）：\n{FEWSHOT}\n\n现在为主题生成正式分镜，直接输出 JSON。"
    body = {
        "model": model_name(),
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user}],
        "temperature": 0.7,
    }
    last_err = None
    for attempt in (1, 2, 3):
        try:
            resp = httpx.post(
                ENDPOINT, json=body,
                headers={"Authorization": f"Bearer {key}"},
                timeout=180,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            data = _extract_json(content)
            pub, plan_scenes, scenes = _validate(data)
            images_dir = task_dir / "images"
            episode = {
                "id": task_id,
                "title": task_id,  # 成片文件名用，保证文件系统安全
                "image_dir": str(images_dir),
                "publish": {**pub["publish"], "desc": pub["publish"]["desc"] or ""},
                "scenes": scenes,
            }
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "episode.json").write_text(
                json.dumps(episode, ensure_ascii=False, indent=2), encoding="utf-8")
            (task_dir / "scenes_plan.json").write_text(
                json.dumps({"output_dir": str(images_dir), "scenes": plan_scenes},
                           ensure_ascii=False, indent=2), encoding="utf-8")
            return episode
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            print(f"[storyboard] 第 {attempt} 次尝试失败：{last_err}", file=sys.stderr)
    raise RuntimeError(f"分镜生成失败（3 次）：{last_err}")
