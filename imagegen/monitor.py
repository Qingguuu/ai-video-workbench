# -*- coding: utf-8 -*-
"""账号监控模块：抓取抖音创作者中心的视频数据，落盘并输出变化日报

方法：Playwright 复用登录态（build/credentials/douyin_state.json，先跑一次 publish.py 登录），
打开内容管理页，拦截页面加载时的 API JSON 响应落盘。
抖音接口字段会随版本变动，v1 先保存原始响应；解析层根据真实数据逐步校准。

用法:
  python monitor.py                 # 抓一次，打印数据快照与上次的差值
"""
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(encoding="utf-8")

MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"
CST = timezone(timedelta(hours=8))

# 常见数据字段名（多套别名兼容，命中即提取）
METRIC_KEYS = {
    "play": ["play_count", "watch_count", "play_cnt", "statistics.play_count"],
    "like": ["digg_count", "like_count", "statistics.digg_count"],
    "comment": ["comment_count", "statistics.comment_count"],
    "share": ["share_count", "statistics.share_count"],
}


def deep_find(obj, keys):
    """在任意嵌套 dict/list 里按别名找第一个命中的值"""
    for k in keys:
        if "." in k:
            a, b = k.split(".", 1)
            if isinstance(obj, dict) and a in obj:
                v = deep_find(obj[a], [b])
                if v is not None:
                    return v
            continue
        if isinstance(obj, dict) and k in obj and isinstance(obj[k], (int, float)):
            return obj[k]
    if isinstance(obj, dict):
        for v in obj.values():
            r = deep_find(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_find(v, keys)
            if r is not None:
                return r
    return None


def main():
    argv = sys.argv
    only_raw = "--raw" in argv
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    mon = cfg.get("monitor", {"platform": "douyin"})
    if mon.get("platform") != "douyin":
        sys.exit(f"v1 暂只支持 douyin，当前: {mon.get('platform')}")

    state_file = os.path.join(ROOT, "build", "credentials", "douyin_state.json")
    if not os.path.exists(state_file):
        sys.exit("没有登录态：先跑一次 publish.py 完成扫码登录")

    data_dir = os.path.join(ROOT, "data", "monitor")
    os.makedirs(data_dir, exist_ok=True)
    now = datetime.now(CST).strftime("%Y%m%d_%H%M%S")

    from playwright.sync_api import sync_playwright

    captured = []

    def on_response(resp):
        try:
            if "creator.douyin.com" not in resp.url:
                return
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                return
            body = resp.json()
            text = json.dumps(body, ensure_ascii=False)
            # 只保留疑似视频数据的大响应，避免登录/配置类噪音
            if any(kw in text for kw in ("video", "item", "aweme", "statistics")) and len(text) > 500:
                captured.append({"url": resp.url, "body": body})
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=state_file, locale="zh-CN",
                                      viewport={"width": 1380, "height": 900})
        page = context.new_page()
        page.on("response", on_response)
        page.goto(MANAGE_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(8000)          # 等列表接口返回
        page.mouse.wheel(0, 4000)            # 触发懒加载，多抓一屏
        page.wait_for_timeout(5000)
        context.storage_state(path=state_file)
        browser.close()

    raw_file = os.path.join(data_dir, f"raw_{now}.json")
    json.dump(captured, open(raw_file, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"原始响应 {len(captured)} 条 → {os.path.relpath(raw_file, ROOT)}")
    if only_raw or not captured:
        return

    # 汇总快照：从捕获响应里提取指标（字段名命中即用）
    snapshot = {}
    for item in captured:
        body = item["body"]
        if not isinstance(body, dict):
            continue
        lists = []
        for k in ("video_list", "aweme_list", "list", "data"):
            v = body.get(k)
            if isinstance(v, list):
                lists.append(v)
            elif isinstance(v, dict):
                lists.extend([x for x in v.values() if isinstance(x, list)])
        for lst in lists:
            for entry in lst:
                if not isinstance(entry, dict):
                    continue
                vid = entry.get("item_id") or entry.get("aweme_id") or entry.get("video_id") \
                    or str(hash(json.dumps(entry, sort_keys=True, default=str)) % 100000)
                m = {name: deep_find(entry, keys) for name, keys in METRIC_KEYS.items()}
                if any(x is not None for x in m.values()):
                    snapshot[str(vid)] = {k: v for k, v in m.items() if v is not None}

    snap_file = os.path.join(data_dir, f"snap_{now}.json")
    json.dump(snapshot, open(snap_file, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"数据快照 {len(snapshot)} 条视频 → {os.path.relpath(snap_file, ROOT)}")

    # 与上一次快照对比
    snaps = sorted(f for f in os.listdir(data_dir) if f.startswith("snap_"))
    if len(snaps) >= 2:
        prev = json.load(open(os.path.join(data_dir, snaps[-2]), encoding="utf-8"))
        print("\n── 与上次对比 ──")
        for vid, cur in snapshot.items():
            old = prev.get(vid)
            if not old:
                continue
            delta = {k: cur.get(k, 0) - old.get(k, 0) for k in cur}
            if any(v > 0 for v in delta.values()):
                nice = "  ".join(f"{k}+{v}" for k, v in delta.items() if v)
                print(f"视频 {vid}: {nice}")
    else:
        print("首次运行，下次开始输出变化日报")


if __name__ == "__main__":
    main()
