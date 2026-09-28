# TeleScope

TeleScope 是一个本地优先的 Telegram 归档与检索工具。Telegram 消息只在用户选择来源并开始同步后写入本机 SQLite；Clash 代理、登录会话、普通搜索、收藏和删除索引都由本地服务管理。

## 安装

需要 Python 3.10+、支持 FTS5 的 SQLite、可访问 Telegram 的代理，以及自己的 Telegram API ID 和 API Hash（在 Telegram 开发者平台申请）。AI 标签搜索为可选功能，需要自行配置兼容的模型端点。

```powershell
git clone https://github.com/YuuseiP/TG-search-tool.git
cd TG-search-tool
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe entry.py
```

上述命令会在 `8000`—`8010` 中选择可用端口，通过就绪检查后打开浏览器。Windows 批处理启动方式见下文；非 Windows 平台安装依赖后运行 `python entry.py`。本地配置、会话、索引和日志保存在忽略版本控制的 `data/` 中，切勿将该目录上传。

## 架构

- `entry.py` 负责选择 `8000` 到 `8010` 之间可用的端口并启动服务。
- `server.py` 提供本地 ASGI 服务。业务应用由 `app.create_app(...)` 创建，测试可以注入临时存储目录以及假 Telegram、假 AI 网关。启动器只有在 `/api/status` 实际返回成功后才会打开浏览器，不会把“端口正在监听”误判成服务已就绪。
- SQLite 保存频道、消息、扫描结果、收藏和 `local_tags`。`message_search` 是消息正文和标签的 FTS5 索引。
- 同一 Telegram 相册只显示一个代表消息；消息仍按 Telegram 消息 ID 增量更新，不下载媒体文件。
- Telegram 的新式 RichMessage 会将文本拆分为页面块，并把图片或文档放入独立资源列表。同步时会归并这些块中的正文、链接和标签，并根据富媒体类型写入图片、视频、音频或文件记录；后续收到的空占位响应不会覆盖已成功归档的内容。对于历史上已写入的空占位记录，可运行 repair_rich_messages.py --data-dir data 进行只增量回填。

首次启动会在应用目录创建 `data/`，其中包含本地数据库、配置和 Telegram 会话文件。测试不会使用这个目录，而是为每个用例创建临时目录。

## 使用流程

1. 按上面的虚拟环境命令启动；或在 Windows 上双击 `启动TeleScope.bat`，它会激活 `tg-history-search` Conda 环境后启动应用。使用批处理前需先创建含 `requirements.txt` 依赖的 Conda 环境；可通过 `TELESCOPE_CONDA_ROOT` 与 `TELESCOPE_CONDA_ENV` 覆盖默认路径和环境名。
2. 在连接设置中填写 Telegram API ID、API Hash、手机号以及 Clash 的 SOCKS5 或 HTTP CONNECT 地址。
3. 完成验证码登录。会话保存在本机，正常重启会复用已保存的登录状态。
4. 扫描当前账号可访问的频道和群组，使用频道/群组分类按需勾选来源后开始同步。
5. 在搜索页使用普通全文、频道、媒体类型、日期和收藏筛选；结果可打开原 Telegram 链接、收藏或取消收藏。

“全部历史消息”会将每个所选来源的同步上限设为无限。完成后，该来源会移入“已同步完整历史”区，保存最大的 Telegram 消息 ID，并在每次应用启动时自动使用该游标读取新消息；“更新已同步频道”可立即触发相同的低消耗增量更新。同步任务按频道报告当前进度、已处理消息数、写入数和错误；相同 Telegram 消息 ID 会更新而不是重复写入。任务状态会持久化，重启后不会丢失已经写入的普通档案。

## 搜索与数据

普通搜索直接使用本地 FTS5 和结构化筛选条件。标签从消息正文中提取到 `local_tags`，`GET /api/tags` 返回完整字典、计数和稳定版本。重复重建字典不会制造新的版本变化；删除频道会级联删除该频道的消息、FTS 记录、收藏和标签计数，但不会删除 Telegram 原消息或其他频道的数据。

迁移现有数据库时会保留普通频道、消息、扫描结果、FTS、收藏和标签数据，并删除旧的 AI 文档、摘要、向量及索引任务表。新架构不存在逐消息 AI 索引后台任务。

## AI 标签搜索

AI 只在用户显式提交自然语言搜索时运行。服务向已配置的本地、云端或混合模型发送搜索词和完整本地 `#tag` 字典，请模型按相关度选择字典中已有标签。消息正文、消息摘要、剧情字段、向量和逐消息 AI 记录不会发送给 AI，也不会写入本地数据库。

返回结果会再次限制为本地字典中的标签。若命中数少于 `minimum_results`，服务按相关度从低到高移除标签，逐步重新执行相同的普通筛选，并返回每一步的标签集合、移除标签和结果计数；达到阈值后立即停止。没有标签命中时，频道、媒体类型和日期等普通筛选仍保持不变。

AI 设置保留 `local`、`cloud` 和 `hybrid` 模式。模型列表需要用户显式请求并按端点缓存；连接测试需要用户显式提交。混合模式先测试本地配置，本地不可用时使用云端配置。应用不会自动下载模型。

## 网络语义

- 读取请求使用严格超时，并只对可证明未收到响应的安全读取执行有限重试。
- 写入请求不自动重放，避免重复同步、重复收藏或重复保存配置。
- 相同资源的并发读取会合并；新的搜索会取消旧请求，只有最新响应可以更新界面。
- 浏览器离线或页面不可见时暂停轮询，恢复在线或重新可见后继续；同步进度只保留一条轮询链。
- Telegram 的所有连接均使用设置中的 Clash 代理。AI 网关只使用用户保存的模式、端点、模型和密钥。

## API 契约

保留的主要路由包括：

- `GET /api/status`、`GET/POST /api/settings`、`POST /api/auth/send-code`、`POST /api/auth/verify`
- `GET /api/dialogs`、`POST /api/channels/scan`、`GET /api/channels`、`DELETE /api/channels/{channel_id}`
- `GET /api/sync`、`POST /api/sync`、`POST /api/sync/incremental`、`GET /api/sync/{job_id}`
- `GET /api/tags`、`POST /api/search`、`POST /api/ai/search`
- `POST /api/messages/{message_id}/bookmark`
- `GET/PUT /api/ai/settings`、`POST /api/ai/models`、`POST /api/ai/test`

`/api/ai/index` 及其子路由不存在。AI 搜索不是索引任务，也不会产生摘要、剧情、向量或逐消息 AI 表。

## 启动与端口

用户启动行为固定在 `8000` 到 `8010`：启动器从这个范围选择第一个可用端口，在 HTTP 就绪探针成功后打开浏览器。手动验证需要使用范围外的临时端口；可设置 `TELESCOPE_DATA_DIR` 为临时目录以隔离验证数据，验证结束后应停止临时服务并确认端口已释放，不要停止用户正在使用的默认服务。

启动器不会调用系统 `py` 或全局 `pip`。它先激活指定的 Conda 环境，依赖安装、服务进程和后续重启均使用该环境中的 `python` 与 `pip`。

运行日志位于 `data/runtime/network.log`。文件记录服务启动阶段、每个 API 请求的路径/状态/耗时、浏览器超时以及 Telegram 代理连接结果；日志不记录 API Hash、代理密码、验证码或模型密钥。日志单文件达到 2 MB 后自动轮转，保留最近 5 个文件。

## 测试

测试只使用 `tempfile.TemporaryDirectory`、内存 ASGI 客户端和注入的假 Telegram/AI 网关，不访问本地运行数据、真实凭据或真实网络。测试覆盖迁移、扫描后选择同步、全部历史、进度持久化、增量写入、链接、相册去重、删除级联、并发保护、收藏、FTS/组合筛选、标签白名单与放宽轨迹、配置、模型发现缓存、超时和混合回退，以及前端请求契约。

```powershell
py -m unittest discover -s tests -v
node --check static/app.js
```

## 排障

- 无法连接 Telegram：确认 Clash 正在运行，并检查代理协议、主机、端口和可选认证信息。
- 扫描结果为空：确认登录状态已保存，并重新执行扫描；扫描只展示当前账号可访问的频道和群组。
- 同步失败：查看同步进度中的频道错误，修正连接后重新选择来源。已经写入的普通消息会被增量更新，不会触发 AI 任务。
- 模型列表或连接测试超时：检查端点、模型名称和网络可达性。读取会在超时后结束，写入请求不会自动重复发送。
- 搜索结果太少：检查频道、媒体、日期和收藏筛选；AI 标签搜索会显示确定性的逐步放宽轨迹。

## 历史富消息修复

旧版本已归档为空占位内容的消息可在关闭同步任务后执行 `python repair_rich_messages.py --data-dir data` 增量回填。执行前请备份 `data/`；修复脚本使用本地 Telegram 会话和已保存的连接设置。
