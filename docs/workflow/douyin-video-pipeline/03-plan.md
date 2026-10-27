# 技术方案与任务拆解：抖音视频制作-发布一体化系统

> 状态：已确认（2026-10-25，方案A）

## 技术选型（方案 A，备选 B）

**A（推荐）：FastAPI + Jinja2 + 原生 JS，零构建链**
- 后端：FastAPI + uvicorn；SQLite（stdlib sqlite3）存任务与发布记录；SSE 推送进度
- 前端：单仓 `templates/` + `static/`，3 个页面（创作 / 任务列表 / 任务详情），无 npm、无打包
- 理由：代码量最小、结构一目了然、无前端工具链负担，符合「完全掌控」

**B（备选）：FastAPI + Vite + React**
- 界面更现代，但引入 node 构建链与组件化复杂度

## 系统结构（新建 `studio/`）

```
studio/
├─ app.py                 # FastAPI 入口与路由
├─ db.py                  # SQLite（tasks / publish_records 两表）
├─ storyboard.py          # 主题 → episode json（调 DashScope qwen 文本模型）
├─ pipeline.py            # 编排器：subprocess 串联 imagegen 四脚本 + 步骤状态落库 + 失败续跑
├─ publisher.py           # 发布封装：调用拷贝来的 douyin_publish.py，结果落库
├─ publish/               # 从 Easel skills/shared/scripts/ 整体拷贝（douyin_publish.py +
│                          # login_state / content_guard / platform_readback / human_pace）
├─ templates/ static/     # 前端页面
├─ data/                  # studio.db + 上传视频
└─ requirements.txt       # fastapi uvicorn httpx python-dotenv（playwright 复用全局安装）
```

imagegen 不拷贝、不修改，以 `cwd=imagegen` subprocess 调用：
`generate.py --file <scenes.json>` → `voice.py --episode <ep.json>` → `subtitle.py --episode <ep.json>` → `video.py --episode <ep.json>`
产物路径固定：`imagegen/build/<ep_id>/<title>.mp4`，clips 有跳过缓存，天然支持续跑。

## 关键设计

1. **分镜生成**（storyboard.py）：读 imagegen 现有 `episodes/ep01.json` 结构作 few-shot，LLM 输出同构 episode json（scenes[].id/image/narration + publish.title/tags）；图片文件名由后端预分配（s01.png…），生图清单 scenes json 一并生成
2. **任务模型**：steps 表记录 5 步（storyboard/image/voice/subtitle/video）+ publish；每步状态/耗时/错误；失败续跑 = 从第一个未完成步骤继续
3. **进度推送**：SSE `/api/tasks/{id}/events`，pipeline 每步变更写库并推事件
4. **发布**：`douyin_publish.py --video <成片> --title <t> --tags <a,b> --exec --status-file <path>`；登录态沿用其 DouyinProfile 持久化目录； headed 模式弹浏览器（扫码场景可见）
5. **上传发布**（F5）：POST /api/upload 存入 data/uploads，走同一预览/发布链路，任务 type=upload
6. **并发控制**：全局同一时刻 1 个制作任务（DB 标志位）；发布任务可独立运行

## 任务顺序

| # | 任务 | 产出 |
|---|---|---|
| 1 | 脚手架：studio 目录、FastAPI 雏形、SQLite schema | 可启动的空壳 |
| 2 | storyboard.py（LLM 分镜） | 主题→episode json |
| 3 | pipeline.py（编排+状态库+续跑） | 一键制作后端闭环 |
| 4 | 任务 API + SSE 进度 | 前端可用接口 |
| 5 | 前端三页（创作/进度、预览/发布编辑、列表） | 可操作 UI |
| 6 | publisher.py + publish/ 拷贝适配 | 发布闭环 + 登录态复用 |
| 7 | 上传发布（F5） | 手动视频发布 |
| 8 | 对照 02-spec 验收标准联调 | 05-verification 输入 |

## 风险

| 风险 | 对策 |
|---|---|
| Easel 发布脚本拷贝后依赖缺失 | 5 个模块整体拷贝，先冒烟 `--help` 验证可独立运行 |
| LLM 输出 json 不合规 | few-shot + json 校验 + 失败重试 2 次 |
| 抖音页面改版选择器失效 | 沿用 Easel 脚本既有三级容错与 debug 快照机制 |
| imagegen/.env 的 DASHSCOPE_API_KEY 失效或未配 | 启动时自检，缺 Key 在页面显著提示 |
| video.py 长耗时超时 | clips 缓存跳过 + 步骤级续跑，超时按失败处理可重试 |
