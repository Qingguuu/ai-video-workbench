# -*- coding: utf-8 -*-
"""平台发布模块（借鉴 social-auto-upload 的方法：Playwright + cookie 持久化）

v1 支持：抖音创作者中心（creator.douyin.com）
首次运行会打开浏览器等你扫码登录，登录态保存到 build/credentials/，之后免扫码。

用法:
  python publish.py --episode episodes/ep01.json            # 上传并填好信息，停在发布前（人工确认）
  python publish.py --episode episodes/ep01.json --yes      # 填好后自动点发布
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(encoding="utf-8")

UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
HOME_URL = "https://creator.douyin.com/creator-micro/home"


def load_json(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return json.load(f)


def wait_upload_page_ready(page, timeout_s):
    """上传页真正可用的判定：主文档或任一 iframe 里出现文件输入框"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for frame in page.frames:
            try:
                if frame.locator("input[type='file']").count() > 0:
                    return True
            except Exception:
                pass
        page.wait_for_timeout(800)
    return False


def wait_logged_in(page, timeout_s=420):
    """打开上传页；若被弹到登录页或「我是创作者/MCN」身份选择落地页，留在窗口里等用户完成。

    关键：判定只认「文件输入框出现」，不看 URL 和登录页特征——
    抖音新会话可能先落到身份选择落地页（二维码藏在 tab 里没展开），
    扫码后可能还要手机验证码，任何中间状态都不能算登录成功。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.goto(UPLOAD_URL, wait_until="domcontentloaded")
        if wait_upload_page_ready(page, 10):
            return True
        print("等待登录/验证完成（请在窗口里扫码，并完成可能出现的手机验证码；不要关窗口）...", flush=True)
        # 若停在身份选择落地页，点开「我是创作者」tab 展示登录二维码
        try:
            tab = page.locator("text=我是创作者").first
            if tab.is_visible(timeout=1500):
                tab.click()
                page.wait_for_timeout(2000)
        except Exception:
            pass
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            if wait_upload_page_ready(page, 1):
                break
    return wait_upload_page_ready(page, 1)


def wait_upload_done(page, timeout_s=600):
    """等视频上传条走完（出现可点击的发布按钮）"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        btn = page.locator("button", has_text="发布")
        try:
            if btn.count() > 0 and btn.first.is_enabled():
                return True
        except Exception:
            pass
        page.wait_for_timeout(2000)
    return False


def fill_desc(page, title, tags):
    """点击描述编辑框，输入标题，再用键盘逐个敲 #话题（触发下拉后回车选中）"""
    editor = page.locator("[contenteditable='true']").first
    editor.click()
    editor.type(title, delay=30)
    for tag in tags:
        editor.type(f" #{tag}", delay=50)
        page.wait_for_timeout(800)          # 等话题下拉出现
        editor.press("Enter")               # 选中第一个联想项
        page.wait_for_timeout(300)
    page.wait_for_timeout(500)


def find_file_input(page, timeout_s=8):
    """在主文档和所有 iframe 里找文件输入框；找不到返回 None"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for frame in page.frames:
            loc = frame.locator("input[type='file']")
            if loc.count() > 0:
                return loc.first
        page.wait_for_timeout(1000)
    return None


def close_popups(page):
    """关掉新人引导/活动浮层，避免挡住上传组件"""
    for sel in ("text=知道了", "text=我知道了", "text=以后再说", "[class*='close-btn']", "[class*='guide'] [class*='close']"):
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=800):
                el.click()
                page.wait_for_timeout(500)
        except Exception:
            pass


def main():
    argv = sys.argv
    auto = "--yes" in argv
    ep_path = argv[argv.index("--episode") + 1] if "--episode" in argv else "episodes/ep01.json"
    episode = load_json(ep_path)
    cfg = load_json("config.json")
    pub = cfg.get("publish", {"platform": "douyin"})
    if pub.get("platform") != "douyin":
        sys.exit(f"v1 暂只支持 douyin，当前: {pub.get('platform')}")

    meta = episode.get("publish", {})
    title = meta.get("title", episode["title"])
    tags = meta.get("tags", ["AI", "科普", "火柴人"])
    video = os.path.join(ROOT, "build", episode["id"], f"{episode['title']}.mp4")
    if not os.path.exists(video):
        sys.exit(f"找不到成片: {video}（先跑 video.py）")

    state_file = os.path.join(ROOT, "build", "credentials", "douyin_state.json")
    browser_name = pub.get("browser", "chromium")
    profile_dir = os.path.join(ROOT, "build", "credentials", f"douyin_profile_{browser_name}")
    os.makedirs(profile_dir, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        # 持久化档案：登录一次永久免扫（cookie+本地存储+指纹都在档案里）
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir, headless=False,
            viewport={"width": 1380, "height": 900}, locale="zh-CN",
        ) if browser_name == "chromium" else p.firefox.launch_persistent_context(
            user_data_dir=profile_dir, headless=False, locale="zh-CN",
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            if wait_logged_in(page):
                print("登录确认：上传页可访问")
            else:
                sys.exit("登录超时（7 分钟），下次重跑会继续保留登录进度")

            print(f"打开上传页并传入视频: {os.path.basename(video)}")
            page.goto(UPLOAD_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            close_popups(page)

            # 上传三级容错：直接 set → 找 iframe 里的 input → 点击上传区捕获 file chooser
            file_input = find_file_input(page)
            if file_input is not None:
                file_input.set_input_files(video)
            else:
                debug_dir = os.path.join(ROOT, "build", episode["id"])
                os.makedirs(debug_dir, exist_ok=True)
                page.screenshot(path=os.path.join(debug_dir, "upload_page_debug.png"), full_page=True)
                open(os.path.join(debug_dir, "upload_page_debug.html"), "w", encoding="utf-8").write(page.content())
                print("未找到文件输入框，已存页面快照，改用点击上传区方式...")
                with page.expect_file_chooser(timeout=15000) as fc_info:
                    for sel in ("[class*='upload']", "text=上传视频", "text=上传"):
                        try:
                            loc = page.locator(sel).first
                            if loc.is_visible(timeout=2000):
                                loc.click()
                                break
                        except Exception:
                            continue
                fc_info.value.set_files(video)

            if not wait_upload_done(page):
                sys.exit("上传超时（10 分钟），请检查网络或手动处理")
            print("视频上传完成，填写标题与话题...")
            fill_desc(page, title, tags)

            if auto:
                page.locator("button", has_text="发布").first.click()
                page.wait_for_timeout(3000)
                print("已点击发布")
            else:
                print("\n信息已填好（未发布）。浏览器里人工检查后点「发布」，")
                print("60 秒后窗口自动关闭。")
                page.wait_for_timeout(60000)
        except Exception as e:
            # 崩溃不关窗：留窗口给人工接手，并存截图供排查
            import traceback
            traceback.print_exc()
            if page.is_closed():
                print("窗口已被手动关闭，脚本退出。")
                sys.exit(1)
            try:
                dbg = os.path.join(ROOT, "build", episode["id"], "publish_error.png")
                os.makedirs(os.path.dirname(dbg), exist_ok=True)
                page.screenshot(path=dbg, full_page=True)
                print(f"出错。浏览器保留 3 分钟供人工接手（也可手动完成上传发布），错误截图: {dbg}")
            except Exception:
                pass
            print(f"异常: {e}")
            page.wait_for_timeout(180000)
        finally:
            context.close()


if __name__ == "__main__":
    main()
