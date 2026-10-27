# 实施变更记录：抖音视频制作-发布一体化系统

> 状态：已实现并联调（2026-10-25）

## 实现结构（与 03-plan 一致，全部落地）

```
studio/
├─ app.py                 # FastAPI 入口：3 页面 + 全部 API + SSE + 视频流式（Range 206）
├─ db.py                  # SQLite 三表（tasks/steps/publish_records）+ 状态机
├─ storyboard.py          # 分镜生成：DashScope OpenAI 兼容端点（qwen-plus），few-shot 对齐 ep01 结构
├─ pipeline.py            # 编排器：cwd=imagegen subprocess 串联 4 脚本 + 步骤落库 + 续跑 + 单任务锁
├─ publisher.py           # 发布封装：whoami/login/publish-video + 状态文件轮询 + 结果落库
├─ publish/               # 从 Easel 整体拷贝的 5 个脚本（douyin_publish + login_state +
│                         #   content_guard + platform_readback + human_pace），仅标准库依赖
├─ templates/ static/     # base/index/task/tasks + style.css + app.js（原生 JS，零构建链）
├─ data/                  # studio.db + tasks/<id>/{episode,scenes_plan}.json + images/ + uploads/
├─ requirements.txt       # fastapi uvicorn jinja2 python-multipart httpx
└─ .env.example
```

## 与计划相比的关键实现决策

| # | 决策 | 原因 |
|---|---|---|
| 1 | 图片产物放 `studio/data/tasks/<id>/images/`（episode 的 image_dir 用绝对路径），voice/字幕/clips/成片仍在 `imagegen/build/<id>/` | imagegen 四脚本的 build 路径在脚本内写死不可改；生成目录可自由指定。成片路径入库，预览接口直接流式服务，不拷贝 |
| 2 | episode.title 固定用 task_id（如 `ep0c8b3f94`），成片名 = `<task_id>.mp4`；发布标题独立存 `publish.title` | LLM 生成的标题可能含 Windows 非法字符，用作文件名有风险 |
| 3 | image 步骤后逐一校验图片存在，缺失即失败；续跑时 scenes_todo 只含缺失分镜 | generate.py 批量模式部分失败仍退出码 0（generate.py:236），不能依赖退出码 |
| 4 | 不写 per-scene style_anchor | generate.py:213 存在循环变量残留 bug，per-scene 覆盖会取到错误值 |
| 5 | publish-video 走 headless；登录单独 `login --headed` 弹窗扫码，状态文件协议 `starting→qr_ready/window_login→sms_required→success/error`，短信验证码经 `douyin.code` 文件回填 | 复用 Easel 的 Web 协议；发布中遇短信墙时失败落库并提示重新发布 |
| 6 | 拷贝的 douyin_publish.py 仅改一行：`PROJECT_ROOT = parents[2]` | 脚本层级变化（skills/shared/scripts → studio/publish），QR/调试 dump 需落回项目根 `outputs/_login/` |
| 7 | SSE 实现 = 服务端每秒轮询 DB 做快照去重推送；终态后连推 3 次同快照即断流 | 无进程内事件总线依赖，多进程/重启场景天然正确 |
| 8 | image 步骤自动注入人设基准图（I2I）：默认 `青瓜瓜/人设参考/_preview/基准图_定稿_单卷须无叶_preview.jpg`，可被 studio/.env 的 `REF_IMAGE` 覆盖，文件不存在则降级纯文生图并在页面提示 | 纯文生图无法锁定角色形象，用户确认画风不一致后接入 I2I；已实测画风与定稿基准图一致 |

## 联调中发现并修复的缺陷

1. **db.py 死锁（严重）**：`update_task`/`update_step`/`add_publish_record` 在 `with _lock:` 内调用 `connect()`，`connect()` 也要拿同一把 `threading.Lock`（不可重入）→ 自死锁，服务整体假死。已改 `threading.RLock`。
2. **单任务并发检查查到自己**：`create_task` 插入的任务即 `status='making'`，`pipeline.start` 的「已有任务」检查命中刚插入的自己 → 409 且任务永久卡 making。已改为排除自身 id。
3. **新版 starlette 的 TemplateResponse 签名**：需 `TemplateResponse(request, name, context)`。

## 外部依赖现状（阻塞项）

- **分镜 LLM（阿里云百炼 qwen-plus）：账户欠费（Arrearage），不可用**。key 位置 `imagegen/.env DASHSCOPE_API_KEY`；需充值或换 key 后，分镜步骤即可正常。可用 `STORYBOARD_MODEL`（studio/.env）换模型。
- 生图（火山引擎 doubao-seedream-5-0-flash）：可用，已冒烟验证。
- TTS（edge-tts）/ffmpeg：可用。
- 抖音登录态：当前未登录。首次发布会引导「扫码登录」（弹有头浏览器），登录态持久化在 `~/.easel-browser-profiles/DouyinProfile`。

## 启动方式

```
cd c:\Users\QingG\Desktop\社媒项目\studio
python -m uvicorn app:app --host 127.0.0.1 --port 8600
```
浏览器打开 http://127.0.0.1:8600/
