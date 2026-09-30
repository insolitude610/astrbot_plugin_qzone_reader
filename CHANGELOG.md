# 更新日志

本插件的重要变更都记在这里。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号以 `metadata.yaml` 里的 `version` 为准。

升级方式：在 AstrBot WebUI 的插件页更新 / 重载插件即可。

## [1.0.2] - 2026-10-01

### 修复

- **纯文字说说读不到正文（只看到开头几十个字）。** 这类分享用的是**另一套链接**：卡片
  `bizsrc=qzone.shuoshuoshareonlytext`、`jumpUrl` 形如
  `mobile.qzone.qq.com/l?g=1336&…ciphertext=…`（不是带图瓜条那种 `h5.qzone.qq.com/ugc/share`）。
  那个 `g=1336` 页面返回 200 HTML，把真正的说说地址写成了**明文 http**：
  `http://mobile.qzone.qq.com/l?g=100&…&res_uin=…&cellid=…`；而 `_resolve_share` 的
  「从 HTML 里找地址」分支**既没有 scheme 校验、也没有主机校验**，于是把这个 http 地址
  原样当成最终地址返回。
  接下来抓它时，凭据门禁（只认 https）不成立 → 请求**不带 Cookie** → QQ 当匿名处理，
  302 到登录页 `i.qq.com` → 分享页解析失败；唯一的兜底接口 `emotion_cgi_msglist_v6`
  又返回 HTTP 500。整条路断掉后，插件降级成卡片文字，而 **QQ 卡片自带的 `desc` 摘要
  本身就是被腾讯截断的**（以 `...` 结尾）—— 所以 bot 只能看到正文开头。
  现在 `_resolve_share` 的两条返回路径都必须过 `normalize_qzone_url`：QQ空间主机的
  `http` 会升级成 `https`，请求重新带上登录态。

### 安全

- 顺手堵上一个缺口：以前分享页 HTML 里出现的**任意主机**地址，只要带着
  `res_uin`/`cellid` 就会被原样当成最终地址。那一跳本身不带 Cookie（响应也不会被解析，
  `_get_html` 本来就拒绝非 QQ空间页面），但页面里**伪造的** `res_uin`/`cellid` 会驱动
  后面那次**带 Cookie** 的兜底列表请求 —— 等于让别人的页面决定 bot 拿登录态去查谁的动态。
  现在只接受白名单主机，这类地址在源头就被丢掉。
- **边界没有放宽**：`is_trusted_qzone_url` / `may_send_credentials` 仍然只认 https，
  卡片里的明文 http 分享链接照旧拒绝。升级只改 scheme、不改主机 —— 白名单主机本来
  就能收 https 凭据，所以凭据可能去的地方一个也没增加。
- `normalize_qzone_url` 对带 userinfo 的地址、非 http(s) 协议一律返回 `None`（fail-closed）。

### 文档

- `DEVELOPMENT.md` 新增「为什么 http 的 QQ空间地址要升级成 https」，附本次线上日志与
  排除过程；补两条排查日志；并记录一条遗留风险：升级之后落点页是否一定含 `FrontPage`
  仍需实测（新日志会给出证据，**不要靠猜地址**）。
- README 补充纯文字说说的链接形态，以及「卡片摘要本来就被 QQ 截断」这一点。
- 修掉 `scripts/check_docs.py` 第 3 项**静默失效**：它用子进程跑自测但没指定输出编码，
  Windows 中文控制台下解码失败，断言数校验一直只打印一句「测试跑不起来」就放过。
  现在给子进程传 `PYTHONIOENCODING=utf-8`，这一项才真的在比对。

### 测试

- 自测从 304 项增至 325 项，新增 `[22h]` 段：升级 / 拒绝 / 幂等 / 端口 / userinfo 等边界，
  以及三条回归 —— HTML 里的明文 http 地址必须被升级、非白名单地址不再被采信、
  `fetch_post` 端到端那次 cell 请求必须走 https 且带 Cookie（这三条在改动前是红的）。

## [1.0.1] - 2026-09-22

### 安全

- **修复：账号登录态可能被发往任意主机。** 此前判断「这是不是 QQ空间分享链接」用的是
  「整条 URL 里是否包含 `qzone.qq.com`」的子串匹配，于是 `https://自己的域名/?x=qzone.qq.com`
  这类地址会被当作分享地址，而插件会带着 `Cookie: uin=…; skey=…; p_skey=…` 去请求它 ——
  等于把账号登录态送给了对方；群里任何人 @bot 时发一句这样的话即可触发。
  现在改为**按解析出的 hostname 走白名单**（只接受 `https` 的 `*.qzone.qq.com`、
  `qzonestyle.gtimg.cn`），Cookie 也只发给白名单内的域名（另含配图床
  `*.qpic.cn`、`*.qlogo.cn`、`*.gtimg.cn`、`*.photo.store.qq.com`）。
- 跳转改为**逐跳校验**：每一跳重新决定请求头，跳转目标不在白名单时既不带 Cookie，
  也不采信其内容。
- `fetch_post()` 入口增加域名门禁。

> 使用 v1.0.0 的话请尽快升级。

### 修复

- `cookie_ttl = 0` 现在真的表示「不缓存」（此前是**永久缓存**，与文档不符）。
- 配置项填了非整数（例如 `max_images: "4张"`）不再出问题：此前会让插件加载失败，
  或让收到的这条消息直接报错。
- 注入文本里的「已附带前 N 张」不再把**切片块数**当成原图张数（那会让模型误以为
  拿到了后面的图）。
- 待注入内容改用随机 token 索引（原先用 `id(event)`），并回收过期条目，
  避免旧内容串进别的消息。
- 只带 `uin=` 的链接在分享页解析失败时不再去别人的动态列表里「按时间戳猜一条」。

### 变更

- `group_whitelist` 改为比对 **AstrBot 的会话 ID（UMO）**，例如
  `aiocqhttp:GroupMessage:123456789`（从 WebUI「会话管理」页面复制）。
  **填纯群号不再生效。** 若开了 `platform_settings.unique_session`，会话 ID 会带上发送者。
- README 补充「说说内容注入到哪条消息里」：两种模式都是追加进**本轮那条用户消息**末尾，
  `keep` 随消息落库，`once` 只存在于当轮请求。

### 文档

- README 增加头图（动图）与徽章；补 `LICENSE`（MIT）与 `logo.png`
  —— 后者同时作为 AstrBot WebUI 的插件图标和**插件市场头图**（文件名由 AstrBot 固定，
  内容为 APNG：真正的 PNG，且保留动画）。
- 说明 `slice_max_height` / `image_max_width` 受 AstrBot 图片长边上限
  （`image_compress_options.max_size`，默认 1280）影响；`once` 模式 + 非视觉模型
  仍会发出图片块等细节。

### 测试

- 自测从 232 项增至 304 项；修掉 GBK 控制台下「全部通过但退出码为 1」的问题。

## [1.0.0] - 2026-09-21

首个版本。

### 新增

- 识别转发 / 粘贴的 QQ空间分享卡片与链接，读取说说完整内容（**含被转发的原文**、
  配图、作者、时间），并把内容注入本轮对话。
- 登录态：通过 NapCat 的 `get_cookies` 自动获取，支持手动 Cookie 兜底、
  带 TTL 缓存、失效自动重取一次。
- 取数优先级：分享页（`FrontPage`）优先，动态列表接口兜底；
  读不到正文时降级用卡片文字，**认不出是哪条说说就放弃，绝不乱猜**。
- 群聊里「先发卡片、再引用它并 @bot」也能触发。
- 配图处理：长截图切片（保持宽度按高度切）、超宽图等比缩放，
  最终张数由 `max_images` 控制。
- 13 个配置项：`cookie_source`、`cookies_str`、`cookie_ttl`、`read_full_feed`、
  `max_images`、`slice_tall_images`、`slice_max_height`、`image_max_width`、
  `jpeg_quality`、`context_mode`、`summarize_mode`、`group_whitelist`、
  `notify_on_failure`。
- 文档与测试：README（使用 / 排障 / 已知限制）、`DEVELOPMENT.md`（开发文档）、
  无 AstrBot 依赖的自测脚本、文档一致性检查脚本。

> 版本号 v1.0.0 从首发一直沿用到 2026-09-22（`4662406`），期间还在同一版本内补充了：
> `image_max_width` 与 `jpeg_quality` 两个图片配置项、`full` 模式要求「正文与配图逐条对应」、
> 叙述规范（第三人称 + 归纳而非缩写）、以及「未被唤醒时不做任何抓取」。
