# -*- coding: utf-8 -*-
"""字幕模块（MoneyPrinterTurbo 方法：用 edge-tts 的逐字时间戳直接对齐，免 Whisper）

用法: python subtitle.py --episode ep01.json
输出: build/ep01/subs.srt  （可导入剪映二次编辑）
      build/ep01/subs.ass  （烧录用，带样式）
时间轴: 镜内 = 词时间戳；镜间偏移 = 前面所有镜的 (配音时长 + gap)，与 video.py 拼接规则一致
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(encoding="utf-8")

PUNCT = "，。？！；：、—…"


def ffprobe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, encoding="utf-8",
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def split_lines(text, max_chars):
    """按标点切行，超长再硬切，产出字幕行列表"""
    lines, buf = [], ""
    for ch in text.strip():
        buf += ch
        if ch in PUNCT or len(buf) >= max_chars:
            lines.append(buf)
            buf = ""
    if buf.strip():
        lines.append(buf)
    return [re.sub(r"\s+", "", l) for l in lines if re.sub(r"\s+", "", l)]


def fmt_srt(t):
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"


def fmt_ass(t):
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def main():
    argv = sys.argv
    ep_path = argv[argv.index("--episode") + 1] if "--episode" in argv else "episodes/ep01.json"
    with open(os.path.join(ROOT, ep_path), encoding="utf-8") as f:
        episode = json.load(f)
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    vcfg = cfg.get("video", {})
    gap = cfg.get("tts", {}).get("gap", 0.3)
    max_chars = vcfg.get("subtitle_max_chars", 14)
    margin_v = vcfg.get("subtitle_margin_v", 320)
    w, h = vcfg.get("width", 1440), vcfg.get("height", 2560)

    base = os.path.join(ROOT, "build", episode["id"])
    voice_dir = os.path.join(base, "voice")

    srt_lines, ass_lines = [], []
    index, offset = 1, 0.0
    for scene in episode["scenes"]:
        sid = scene["id"]
        mp3 = os.path.join(voice_dir, f"{sid}.mp3")
        words = json.load(open(os.path.join(voice_dir, f"{sid}.words.json"), encoding="utf-8"))
        audio_dur = ffprobe_duration(mp3)

        for line in split_lines(scene["narration"], max_chars):
            need = len(re.sub(rf"[{PUNCT}\s]", "", line))
            start_local, end_local, got = None, None, 0
            while words and got < need:
                wd = words.pop(0)
                if start_local is None:
                    start_local = wd["t"]
                end_local = wd["t"] + wd["d"]
                got += len(wd["w"])
            if start_local is None:
                continue
            if words == [] and got < need:
                end_local = audio_dur  # 词边界耗尽，兜底到配音结尾
            start, end = offset + start_local, offset + end_local
            text = line.rstrip(PUNCT)
            srt_lines.append(f"{index}\n{fmt_srt(start)} --> {fmt_srt(end)}\n{text}\n")
            ass_lines.append(f"Dialogue: 0,{fmt_ass(start)},{fmt_ass(end)},Sub,,0,0,0,,{{\\fad(80,80)}}{text}")
            index += 1
        offset += audio_dur + gap

    open(os.path.join(base, "subs.srt"), "w", encoding="utf-8-sig").write("\n".join(srt_lines))
    ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub, Microsoft YaHei, {int(h * 0.026)}, &H00FFFFFF, &H000000FF, &H00000000, &H64000000, 0, 0, 0, 0, 100, 100, 0, 0, 1, 4, 1, 2, 80, 80, {margin_v}, 1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    open(os.path.join(base, "subs.ass"), "w", encoding="utf-8-sig").write(
        ass_header + "\n".join(ass_lines) + "\n")
    print(f"字幕完成：{index - 1} 条，总时长约 {offset:.1f}s → {base}\\subs.srt / subs.ass")


if __name__ == "__main__":
    main()
