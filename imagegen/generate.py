# -*- coding: utf-8 -*-
"""青瓜瓜批量生图脚本（多 provider：阿里云百炼 qwen-image / 火山引擎 Seedream）

用法：
  单张（纯文生图）   python generate.py --prompt "画面描述" --out outputs/test.png
  单张（基准图参考） python generate.py --prompt "画面描述" --ref 基准图.png --out outputs/test.png
  批量               python generate.py --file scenes.json

说明：
  - provider 在 config.json 里切换（bailian / volcano），模型与尺寸随 provider 走
  - 每条 prompt 会自动拼接 config.json 里的 style_anchor（画风锚点）
  - --ref 可重复传入最多 3 张参考图（图生图 I2I），角色一致性要求高的分镜建议都带基准图
  - 批量模式：scenes.json 里可配全局 "ref_images": ["路径"]，
    也可在单个 scene 里用 "ref_images" 覆盖
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
# 各 provider 的生图端点与所需的环境变量名
PROVIDER_API = {
    "bailian": "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
    "volcano": "https://ark.cn-beijing.volces.com/api/v3/images/generations",
}
PROVIDER_KEY_ENV = {
    "bailian": "DASHSCOPE_API_KEY",
    "volcano": "ARK_API_KEY",
}

# Windows 控制台中文输出兜底
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


def load_env():
    """读取 .env 文件里的 KEY=VALUE，不覆盖已存在的环境变量"""
    env_path = os.path.join(ROOT, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def load_config():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def to_data_uri(path):
    """本地图片转 base64 data URI（参考图输入格式）"""
    if not os.path.isabs(path):
        path = os.path.join(ROOT, path)
    mime = MIME_MAP.get(os.path.splitext(path)[1].lower(), "image/png")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{data}"


def generate_image(prompt, cfg, api_key, ref_images=None):
    """按当前 provider 调用生图 API，返回图片 URL。ref_images 为本地参考图路径列表"""
    provider = cfg.get("provider", "bailian")
    pcfg = cfg["providers"][provider]

    if provider == "volcano":
        # 火山方舟 images/generations 协议
        body = {
            "model": pcfg["model"],
            "prompt": prompt,
            "response_format": "url",
            "size": pcfg["size"],
            "watermark": cfg.get("watermark", False),
        }
        if ref_images:
            body["image"] = [to_data_uri(p) for p in ref_images]
    else:
        # DashScope 多模态端点协议（qwen-image）
        parameters = {
            "size": pcfg["size"],  # DashScope 协议用星号分隔，如 864*1536
            "n": cfg.get("n", 1),
            "prompt_extend": cfg.get("prompt_extend", True),
            "watermark": cfg.get("watermark", False),
        }
        if cfg.get("negative_prompt"):
            parameters["negative_prompt"] = cfg["negative_prompt"]
        content = []
        for img_path in (ref_images or []):
            content.append({"image": to_data_uri(img_path)})
        content.append({"text": prompt})
        body = {
            "model": pcfg["model"],
            "input": {
                "messages": [
                    {"role": "user", "content": content}
                ]
            },
            "parameters": parameters,
        }

    req = urllib.request.Request(
        PROVIDER_API[provider],
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    # 生图可能需要 10-90 秒，超时给足
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    if provider == "volcano":
        # 响应结构：data[0].url
        items = result.get("data") or []
        if items and items[0].get("url"):
            return items[0]["url"]
        raise RuntimeError(f"响应中没有图片: {json.dumps(result, ensure_ascii=False)[:300]}")

    # 百炼响应结构：output.choices[0].message.content 数组里找 image 项
    msg_content = result["output"]["choices"][0]["message"]["content"]
    for item in msg_content:
        if "image" in item:
            return item["image"]
    raise RuntimeError(f"响应中没有图片: {json.dumps(result, ensure_ascii=False)[:300]}")


def download_image(url, path):
    """下载图片到本地，URL 有效期只有 24 小时，必须及时保存"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as resp:
        with open(path, "wb") as f:
            f.write(resp.read())


def resolve_refs(refs):
    """校验参考图列表：最多 3 张，路径不存在直接报错"""
    if not refs:
        return None
    if len(refs) > 3:
        raise SystemExit("错误：qwen-image-3.0 最多支持 3 张参考图")
    for p in refs:
        full = p if os.path.isabs(p) else os.path.join(ROOT, p)
        if not os.path.exists(full):
            raise SystemExit(f"错误：参考图不存在 {full}")
    return refs


def main():
    load_env()
    cfg = load_config()
    provider = cfg.get("provider", "bailian")
    key_env = PROVIDER_KEY_ENV[provider]
    api_key = os.environ.get(key_env)
    if not api_key:
        sys.exit(f"错误：缺少 {key_env}，请在 .env 里配置（当前 provider: {provider}）")

    parser = argparse.ArgumentParser(description="青瓜瓜批量生图")
    parser.add_argument("--prompt", help="单张模式的画面描述")
    parser.add_argument("--out", help="单张模式的保存路径", default="outputs/single.png")
    parser.add_argument("--file", help="批量模式的分镜清单 json")
    parser.add_argument("--ref", action="append", help="参考图路径（图生图模式，可重复，最多 3 张）")
    args = parser.parse_args()

    # 组装任务列表：(编号, 画面描述, 保存路径, 参考图列表)
    tasks = []
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            plan = json.load(f)
        out_dir = plan.get("output_dir", "outputs/batch")
        global_refs = plan.get("ref_images")
        for scene in plan["scenes"]:
            refs = scene.get("ref_images", global_refs)
            tasks.append((scene["id"], scene["prompt"],
                          os.path.join(out_dir, f"{scene['id']}.png"), refs))
    elif args.prompt:
        tasks.append(("single", args.prompt, args.out, args.ref))
    else:
        parser.print_help()
        return

    ok, fail = 0, 0
    for i, (sid, desc, path, refs) in enumerate(tasks, 1):
        try:
            refs = resolve_refs(refs)
        except SystemExit as e:
            print(f"[{i}/{len(tasks)}] {sid} {e}")
            fail += 1
            continue

        mode = f"图生图×{len(refs)}" if refs else "文生图"
        # 分镜级锚点覆盖：scene 里带 "style_anchor" 时优先用它的
        anchor = scene.get("style_anchor", cfg.get("style_anchor", ""))
        prompt = f"{desc}；{anchor}" if anchor else desc
        print(f"[{i}/{len(tasks)}] {sid} 生成中（{mode}，{cfg['provider']}/{cfg['providers'][cfg['provider']]['model']}）…")
        for attempt in (1, 2):  # 失败自动重试一次
            try:
                url = generate_image(prompt, cfg, api_key, refs)
                download_image(url, path)
                print(f"    已保存 {path}")
                ok += 1
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")[:300]
                print(f"    HTTP {e.code}: {detail}")
            except Exception as e:
                print(f"    出错: {e}")
            if attempt == 2:
                print("    重试仍失败，跳过该张")
                fail += 1
            else:
                print("    2 秒后重试…")
                time.sleep(2)
        time.sleep(1)  # 轻微限速，避免连续请求

    print(f"\n完成：成功 {ok} 张，失败 {fail} 张")


if __name__ == "__main__":
    main()
