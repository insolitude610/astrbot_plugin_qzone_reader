# astrbot_plugin_qzone_reader

转发 QQ空间说说给 bot，自动读取、总结，并且**内容会进入会话历史**，之后可以继续追问讨论。

## 它解决什么问题

QQ空间说说的分享卡片本身只带一句标题和摘要，模型看不到正文。这个插件在你转发卡片时：

1. 从卡片（或文本链接）里认出 QQ空间说说地址；
2. 通过 NapCat 的登录态调用 QQ空间接口，拉取该条说说的**完整正文、配图、作者、时间**；
3. 在**生成回复之前**把内容作为「额外用户内容块」注入本轮请求。

关键点在于第 3 步的注入方式：AstrBot 会把「用户消息 + 额外内容块 + 图片」组装成**同一条 user 消息**并写入会话历史。
所以说说内容不是一次性提示，而是真正留在了上下文里 —— 你下一句直接问「那你怎么看」它依然记得。

## 安装

把整个目录放到 AstrBot 的插件目录下：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/SolitudeRA/astrbot_plugin_qzone_reader
# 或在 WebUI 插件页用「上传插件文件」/ 填写仓库地址安装
```

依赖只有 `aiohttp`（AstrBot 本体已自带），无需额外 `pip install`。装好后在 WebUI 重载插件即可。

## 使用

**不需要任何指令。** 直接把说说转发给 bot：

1. 在 QQ空间 App 里点说说的「分享」→ 发送给 bot；
2. bot 自动读取并总结；
3. 接着追问即可，比如「作者什么情绪」「帮我写个类似风格的」。

也支持直接把 `https://h5.qzone.qq.com/ugc/share?...` 链接粘贴给它。

## 登录态从哪来

QQ空间没有开放接口，读说说必须带账号 Cookie。插件按以下顺序取：

1. **NapCat 自动获取**（默认）：通过 OneBot 的 `get_cookies(domain="user.qzone.qq.com")` 直接问协议端要，成功后会缓存（默认 600 秒，见 `cookie_ttl`）。
2. **手动 Cookie 兜底**：把 `cookie_source` 设为 `manual`，或自动获取失败时使用 `cookies_str`。

抓手动 Cookie 的方法：电脑浏览器登录 QQ空间 → F12 → Network → 刷新 → 任意请求 → 复制请求头里的完整 `Cookie`，
其中必须包含 `uin`、`skey`、`p_skey`。

> Cookie 等同于账号密码。它只存在你的 AstrBot 配置里，不会外发；请勿把它贴到公开群聊或截图分享。

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `cookie_source` | `napcat` | `napcat` 走协议端自动获取；`manual` 只用 `cookies_str` |
| `cookies_str` | 空 | 手动 Cookie，任何模式下都作为兜底 |
| `cookie_ttl` | `600` | 自动获取的 Cookie 缓存秒数，`0` 表示不缓存 |
| `read_full_feed` | `true` | 关闭后只用卡片上的标题/摘要，不发接口请求 |
| `max_images` | `4` | 附带几张配图给多模态模型；`0` 表示不带图（纯文本模型建议设 0） |
| `auto_summarize` | `true` | 注入时附加总结指令，实现「转发即自动总结」 |
| `group_whitelist` | 空 | 只在这些会话生效（群号/QQ号），留空为全部 |
| `notify_on_failure` | `true` | 读取失败时告知用户，而不是让模型编内容 |

## 工作原理

```
转发卡片 ──► 识别 (Comp.Json.data / 纯文本 / raw_message 兜底)
              │  递归找 h5.qzone.qq.com/ugc/share，并还原 &#44; 转义
              ▼
          取登录态 (NapCat get_cookies → 手动 Cookie)
              ▼
          拉取正文 (emotion_cgi_msglist_v6 列表 → 定位 cellid → msgdetail_v6 取全文)
              ▼
          on_llm_request 注入 extra_user_content_parts + image_urls
              ▼
          AstrBot 组装为同一条 user 消息 ──► 写入会话历史 ──► 可继续讨论
```

## 排障

- **bot 说读不到内容**：多为登录态失效。先确认 NapCat 在线且 QQ 未掉线，然后在 AstrBot 日志里搜 `[qzone_reader]`。
  实在取不到就手动填 `cookies_str`。
- **总结里没有图片内容**：模型不支持图片输入时 AstrBot 会跳过图片；把 `max_images` 设为 `0` 让插件只发文字。
- **只对某些群生效**：用 `group_whitelist` 填群号。
- **想关掉自动总结**：`auto_summarize` 设为 `false`，插件仍会注入原文，由你决定问什么。

## 已知限制

- 依赖 aiocqhttp / OneBot v11（NapCat 等），其他平台适配器不生效。
- 只能读取**你的账号有权限看到**的说说；好友可见、仅自己可见都依赖登录态，陌生人隐私内容读不到。
- 视频只记录「含视频」，不解析视频内容。
- 用的是 QQ空间 Web 端接口，属于非公开接口，腾讯改结构时可能需要跟进修复。

## 许可

MIT
