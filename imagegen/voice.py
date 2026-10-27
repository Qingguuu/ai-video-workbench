# -*- coding: utf-8 -*-
"""配音模块（参考 MoneyPrinterTurbo / Stickman-Studio 的 edge-tts 方案）

按 episode.json 逐镜生成配音，并记录逐字时间戳（用于免 Whisper 对齐字幕）。
用法: python voice.py --episode ep01.json
输出: build/ep01/voice/<镜ID>.mp3        配音音频
      build/ep01/voice/<镜ID>.words.json 逐字时间戳
"""
import asyncio
import json
import os
import sys

import edge_tts

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(encoding="utf-8")


def load_config():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def apply_replacements(text, tts_cfg):
    """发音替换表：配音层读谐音（如 AI→欸爱），字幕层仍显示原文"""
    for k, v in tts_cfg.get("replacements", {}).items():
        text = text.replace(k, v)
    return text


async def synth_one(text, voice, rate, mp3_path, words_path):
    timestamps = []
    communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    with open(mp3_path, "wb") as audio:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                timestamps.append({
                    "t": chunk["offset"] / 1e7,        # 词开始（秒）
                    "d": chunk["duration"] / 1e7,      # 词时长（秒）
                    "w": chunk["text"],
                })
    with open(words_path, "w", encoding="utf-8") as f:
        json.dump(timestamps, f, ensure_ascii=False)
    return timestamps


def main():
    parser_arg = sys.argv
    ep_path = parser_arg[parser_arg.index("--episode") + 1] if "--episode" in parser_arg else "episodes/ep01.json"
    with open(os.path.join(ROOT, ep_path), encoding="utf-8") as f:
        episode = json.load(f)

    cfg = load_config()
    tts_cfg = cfg.get("tts", {})
    if tts_cfg.get("provider", "edge") != "edge":
        sys.exit(f"暂只支持 edge provider，当前配置: {tts_cfg.get('provider')}")

    voice = tts_cfg.get("voice", "zh-CN-YunjianNeural")
    rate = tts_cfg.get("rate", "+0%")
    out_dir = os.path.join(ROOT, "build", episode["id"], "voice")
    os.makedirs(out_dir, exist_ok=True)

    async def run():
        for i, scene in enumerate(episode["scenes"], 1):
            sid = scene["id"]
            mp3 = os.path.join(out_dir, f"{sid}.mp3")
            words = os.path.join(out_dir, f"{sid}.words.json")
            if os.path.exists(mp3) and os.path.exists(words):
                print(f"[{i}/{len(episode['scenes'])}] {sid} 已存在，跳过")
                continue
            ts = await synth_one(apply_replacements(scene["narration"], tts_cfg), voice, rate, mp3, words)
            print(f"[{i}/{len(episode['scenes'])}] {sid} 完成（{len(ts)} 个词边界）")

    asyncio.run(run())
    print(f"配音完成 → {out_dir}")


if __name__ == "__main__":
    main()
