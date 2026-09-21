# astrbot_plugin_qzone_reader

把 QQ空间说说转发给 bot，自动读取、总结，并且**内容会进入会话历史**，之后可以继续追问讨论。

不需要任何指令 —— 转发动作本身就是触发条件。

## 为什么需要它

QQ空间说说的分享卡片上只有一句标题和摘要，模型看不到正文。**转发（repost）更麻烦**：卡片上能拿到的往往只有转发者自己写的那句短评语，被转发的原文完全不在卡片里。

典型场景：

> 某人转发了一篇瓜条，只写了句「现在的小朋友玩的真花」。
> 卡片上只有这 12 个字，bot 完全不知道那篇瓜条讲了什么。

本插件会把说说（**含被转发的原文**）的完整正文、配图、作者、时间取出来，注入本轮对话。

## 安装

在 AstrBot 的插件目录下：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/insolitude610/astrbot_plugin_qzone_reader
```

也可以在 WebUI 插件页填写仓库地址安装，或上传插件压缩包。

依赖只有 `aiohttp`，AstrBot 本体已自带，**无需额外 pip install**。装好后在 WebUI 重载插件即可。

## 使用

把说说转发给 bot：

1. 在 QQ空间里点说说的「分享」→ 发送给 bot；
2. bot 自动读取并总结；
3. 接着追问即可，比如「作者是什么情绪」「帮我梳理一下时间线」。

也支持直接粘贴链接：`https://h5.qzone.qq.com/ugc/share?...` 或 `https://mobile.qzone.qq.com/l?...`

### 转发的说说

转发场景下内容会分两层呈现，bot 两层都能看到：

```
转发者：XXX          转发时间：...
转发语：现在的小朋友玩的真花

—— 以下是这条说说转发的原内容 ——
原文作者：YYY        原文发布时间：...
原文正文：（完整原文）
原文配图：共 N 张
```

配图会优先附带**原文的图**，因为转发场景下原文图才是主体。

## 登录态从哪来

QQ空间没有开放接口，读取说说需要账号 Cookie。插件按以下顺序尝试：

1. **NapCat 自动获取**（默认）
   通过 OneBot 的 `get_cookies(domain="user.qzone.qq.com")` 直接向协议端索取，成功后缓存（默认 600 秒，见 `cookie_ttl`）。登录态失效时会自动清缓存重取一次。
2. **手动 Cookie 兜底**
   把 `cookie_source` 设为 `manual`，或在自动获取失败时使用 `cookies_str`。

抓手动 Cookie：电脑浏览器登录 QQ空间 → F12 → Network → 刷新 → 任意请求 → 复制请求头里的完整 `Cookie`，必须包含 `uin`、`skey`、`p_skey`。

> **Cookie 等同于账号密码。** 它只保存在你的 AstrBot 配置里，不会外发，也建议限制 `group_whitelist` 避免群友借用你的登录态。请勿贴到公开群聊或截图分享。

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `cookie_source` | `napcat` | `napcat` 走协议端自动获取；`manual` 只用 `cookies_str` |
| `cookies_str` | 空 | 手动 Cookie，任何模式下都作为兜底 |
| `cookie_ttl` | `600` | 自动获取的 Cookie 缓存秒数，`0` 表示不缓存 |
| `read_full_feed` | `true` | 关闭后只用卡片上的文字，不发任何网络请求 |
| `max_images` | `4` | 附带几张配图给多模态模型；`0` 表示不带图（纯文本模型建议设 `0`） |
| `auto_summarize` | `true` | 注入时附上总结指令，实现「转发即自动总结」 |
| `group_whitelist` | 空 | 只在这些会话生效（群号/QQ号），留空为全部会话 |
| `notify_on_failure` | `true` | 读不到正文时是否告知用户。关闭则静默降级，只把卡片文字交给模型且不提失败 |

## 工作原理

```
转发卡片 / 粘贴链接
      │
      ▼
① 识别链接   Comp.Json.data / Plain 文本 / raw_message 兜底
      │        （递归扫描，并还原 QQ 卡片里的 &#44; 转义）
      ▼
② 取登录态   NapCat get_cookies → 手动 Cookie
      │
      ▼
③ 短链解析   跟随 302 跳转到 h5.qzone.qq.com/ugc/share
      │        拿到 res_uin / cellid
      ▼
④ 读分享页   解析页面内嵌的 FrontPage 数据
      │        cell_summary 正文 · cell_pic 配图 · cell_original 被转发的原文
      │        （失败则退回 emotion_cgi_msglist_v6 列表接口）
      ▼
⑤ 注入       on_llm_request → extra_user_content_parts + image_urls
      │
      ▼
   AstrBot 组装为同一条 user 消息 → 写入会话历史 → 可继续讨论
```

第 ⑤ 步是「能持续讨论」的关键：AstrBot 会把「用户消息 + 额外内容块 + 图片」组装成**同一条 user 消息**并写入会话历史。所以说说内容不是一次性提示，而是真正留在了上下文里。

## 排障

先在 AstrBot 日志里搜 `[qzone_reader]`，插件的关键步骤都会打日志。

| 现象 | 原因与处理 |
| --- | --- |
| 日志出现 `没有可用的 QQ空间登录态` | NapCat 未在线或 QQ 已掉线。恢复登录，或手动填 `cookies_str` |
| 日志出现 `放弃读取` | 分享链接里没有可定位说说的参数。把日志里那行链接发出来以便跟进 |
| 日志出现 `分享页没取到内容，改走动态列表接口兜底` | 分享页未返回数据（权限较严的说说）。若列表接口也定位不到，会降级为卡片文字 |
| bot 说读不到内容 | 多为登录态失效，按第一行处理 |
| 总结里没有图片内容 | 模型不支持图片输入时 AstrBot 会跳过图片；把 `max_images` 设为 `0` 让插件只发文字 |
| 只在某些群生效 | 用 `group_whitelist` 填群号 |
| 不想每次都被回「没读到」 | 把 `notify_on_failure` 设为 `false` |
| 上下文被长瓜条占满 | 调小 `max_images`；说说正文本身无法截断，这是设计取舍 |

## 已知限制

- 依赖 **aiocqhttp / OneBot v11**（NapCat、LLOneBot 等），其他平台适配器不生效。
- 只能读取**你的账号有权限看到**的内容；登录态失效时读不到好友可见的说说。
- 分享页对部分权限较严的说说可能不返回数据，此时会退回列表接口；两条路都失败则降级为卡片文字。
- 视频只记录「含视频 N 个」，不解析视频内容。
- 长文本瓜条注入后会占用较多上下文，受模型上下文长度限制。
- 使用的是 QQ空间 Web 端非公开接口，腾讯调整页面结构时可能需要跟进修复。

## 许可

MIT
