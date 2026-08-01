# TeleScope

TeleScope 是一个本地 Telegram 频道归档和搜索工具。它使用 Telegram 官方客户端 API，通过 Clash 代理连接；会话、配置和索引都只保存在当前电脑的 `data/` 目录，不会上传到其他服务。

## 启动

Windows 下直接双击 `启动TeleScope.cmd`。手动启动：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
py server.py
```

启动入口会自动选择 `8000` 到 `8010` 之间未被占用的本地端口，并打开浏览器。

## 使用流程

1. 在 `my.telegram.org/apps` 创建应用，填写 API ID、API Hash、手机号和 Clash 代理信息。默认代理为 `SOCKS5 127.0.0.1:7890`。
2. 首次完成验证码登录后，Telegram 将授权信息写入 `data/telegram.session`。正常关闭并重新启动后会复用该会话，不需要再次发送验证码。
3. 打开“频道管理”，点击“扫描频道”。扫描只读取当前账号可访问的频道和群组名称及标识，不读取消息，也不建立索引。
4. 勾选需要的频道，点击“获取并建立索引”。界面会显示当前频道、已读取消息数、已完成频道数和每个频道的结果。

## 同步内容

同步不是下载频道文件。它将消息正文或媒体说明文字、发送时间、发送者 ID、媒体类型、标签、公开链接和回复关系写入 `data/telescope.db`，再由 SQLite FTS5 建立全文索引。图片、视频、音频和文档只记录类型；当前实现没有调用 `download_media`，因此不会把这些二进制文件下载到本机。

每条索引消息都会保存可跳转的 Telegram 链接：公开频道使用 `https://t.me/<username>/<message-id>`，私有频道或超级群使用 `https://t.me/c/<internal-channel-id>/<message-id>`。后者只会在当前已登录账号具有访问权限时打开。

## 搜索

- 普通关键词会在消息正文和说明文字中全文匹配。
- `#tag` 会作为标签筛选条件；多个标签需要同时匹配。
- 可以组合文本、图片、视频、文件、音频和链接类型，以及频道和日期范围。
- 搜索和收藏都只操作本地 SQLite 索引。

`data/config.json`、`data/telegram.session` 和 `data/telescope.db` 均包含私有数据，不应分享或提交到版本库。
