# -*- coding: utf-8 -*-
"""成片模块（Stickman-Studio / Zenn 类频道的 FFmpeg slideshow 方法）

每镜 = 静态图 zoompan 动效 + 该镜配音，视频时长 = 配音时长 + gap，
逐镜编码后 concat，时间轴与字幕严格对齐；最后烧录 ASS 字幕。
用法: python video.py --episode ep01.json
输出: build/ep01/clips/*.mp4 → merged.mp4 → <title>.mp4（成片）
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(encoding="utf-8")


def ffprobe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, encoding="utf-8",
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if r.returncode != 0:
        print(r.stderr[-1200:])
        sys.exit(f"命令失败: {' '.join(cmd[:6])}...")


def main():
    argv = sys.argv
    ep_path = argv[argv.index("--episode") + 1] if "--episode" in argv else "episodes/ep01.json"
    with open(os.path.join(ROOT, ep_path), encoding="utf-8") as f:
        episode = json.load(f)
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    gap = cfg.get("tts", {}).get("gap", 0.3)
    fps = cfg.get("video", {}).get("fps", 24)
    w = cfg.get("video", {}).get("width", 1440)
    h = cfg.get("video", {}).get("height", 2560)
    w2, h2 = w * 2, h * 2  # 先放大再 zoompan，避免抖动

    base = os.path.join(ROOT, "build", episode["id"])
    clip_dir = os.path.join(base, "clips")
    os.makedirs(clip_dir, exist_ok=True)

    clips = []
    for i, scene in enumerate(episode["scenes"]):
        sid = scene["id"]
        img = os.path.join(ROOT, episode.get("image_dir", "outputs"), scene["image"])
        mp3 = os.path.join(base, "voice", f"{sid}.mp3")
        clip = os.path.join(clip_dir, f"{sid}.mp4")
        clips.append(clip)
        if os.path.exists(clip):
            print(f"[{i + 1}/{len(episode['scenes'])}] {sid} 已存在，跳过")
            continue
        dur = round(ffprobe_duration(mp3) + gap, 3)
        frames = max(2, round(dur * fps))
        if i % 2 == 0:  # 交替运镜：偶数缓慢推近，奇数固定放大下摇
            motion = (f"zoompan=z='min(zoom+0.00035,1.09)':"
                      f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}")
        else:
            motion = (f"zoompan=z='1.12':"
                      f"x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*on/{frames}':d={frames}")
        vf = (f"[0:v]scale={w2}:{h2}:force_original_aspect_ratio=increase,"
              f"crop={w2}:{h2},{motion}:s={w}x{h}:fps={fps}[v]")
        run(["ffmpeg", "-y", "-i", img, "-i", mp3,
             "-filter_complex", vf, "-map", "[v]", "-map", "1:a",
             "-af", f"apad=whole_dur={dur}", "-t", str(dur),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", clip])
        print(f"[{i + 1}/{len(episode['scenes'])}] {sid} 编码完成（{dur:.1f}s）")

    merged = os.path.join(base, "merged.mp4")
    list_path = os.path.join(clip_dir, "list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for c in clips:
            f.write(f"file '{os.path.relpath(c, clip_dir)}'\n")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", merged])
    print(f"拼接完成 → merged.mp4")

    final = os.path.join(base, f"{episode['title']}.mp4")
    run(["ffmpeg", "-y", "-i", merged,
         "-vf", f"ass={os.path.relpath(os.path.join(base, 'subs.ass'), ROOT).replace(os.sep, '/')}",
         "-c:a", "copy", final])
    total = ffprobe_duration(final)
    print(f"成片完成：{final}（{total:.1f}s / {total / 60:.1f} 分钟）")


if __name__ == "__main__":
    main()
