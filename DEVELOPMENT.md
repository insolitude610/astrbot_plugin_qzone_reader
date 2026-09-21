# 开发文档

面向维护者。用户使用说明见 [README.md](README.md)。

## 目录

- [整体思路](#整体思路)
- [模块结构](#模块结构)
- [数据流](#数据流)
- [API 参考](#api-参考)
- [FrontPage 数据结构](#frontpage-数据结构)
- [关键设计决策](#关键设计决策)
- [两个必须知道的 AstrBot 陷阱](#两个必须知道的-astrbot-陷阱)
- [测试](#测试)
- [调试](#调试)
- [扩展指引](#扩展指引)
- [已知脆弱点](#已知脆弱点)

## 整体思路

插件做三件事，顺序不能颠倒：

1. **在消息阶段**认出 QQ空间分享链接，**提前**把内容取回来；
2. **在 LLM 请求阶段**把内容注入请求；
3. 让 AstrBot 自己去落历史 —— 插件**不碰**会话历史。

第 1 步和第 2 步必须拆开，原因是注入只能发生在 `on_llm_request`，而那时事件已经定型；网络请求放在那里会阻塞 LLM 请求链路，所以提前取好、暂存在插件里。

第 3 步是「能持续讨论」的关键：**不要自己写历史**。AstrBot 会把 `req.prompt` + `req.extra_user_content_parts` + `req.image_urls` 组装成同一条 user 消息，再由 `_save_to_history()` 落库。注入的内容因此天然进入历史，无需额外处理。

## 模块结构

```
astrbot_plugin_qzone_reader/
├── main.py                    # 插件入口：卡片识别、登录态获取、注入
├── core/
│   ├── __init__.py            # 包说明
│   ├── frontpage.py           # 解析 h5 分享页内嵌的 FrontPage 数据
│   ├── images.py              # 长截图切片与配图预算控制
│   └── qzone_api.py           # 数据模型、链接识别、HTTP 调用、降级逻辑
├── tests/fixtures/
│   └── repost_cell.json       # 脱敏后的转发说说数据，供回归测试用
├── test_core.py               # 自测脚本（无 AstrBot 依赖，见「测试」）
├── _conf_schema.json          # 配置项定义
└── metadata.yaml              # 插件元数据
```

职责边界：

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| `main.py` | AstrBot 交互、事件过滤、登录态来源、注入、降级决策、配图预算 | 任何 HTML/JSON 解析、图片编解码 |
| `core/qzone_api.py` | 链接识别、参数提取、HTTP、降级编排、渲染成文本 | AstrBot 相关逻辑、图片处理 |
| `core/frontpage.py` | 纯函数：从 HTML 里解出说说数据结构 | 网络、AstrBot |
| `core/images.py` | 图片下载、长截图切片、按预算挑选 | 业务判断、AstrBot |

`core/frontpage.py` 刻意做成**零依赖纯函数模块**，可以脱离整个插件单独测试。
`core/images.py` 是唯一依赖 Pillow 的模块，且**延迟导入**：缺库时只降级为发原图，不影响插件加载。

## 数据流

```
① 消息事件
   capture_qzone_share(event)
     ├─ _in_scope(event)              会话白名单
     ├─ _find_share_url(event)        识别链接
     │    ├─ Comp.Json.data           ← 主路径（分享卡片）
     │    ├─ Comp.Plain 文本          ← 直接粘贴链接
     │    ├─ event.message_str
     │    └─ message_obj.raw_message  ← 兜底（适配器未解析的卡片类型）
     ├─ _build_payload(event, url)    取内容
     │    ├─ _get_credentials()       登录态（带缓存）
     │    └─ _fetch_with_retry()      → fetch_post()（见下）
     └─ self._pending[id(event)] = (text, images)   暂存

② LLM 请求钩子
   inject_qzone_content(event, req)
     ├─ self._pending.pop(id(event))  取出并清理暂存
     ├─ _platform_ok(event)           手动平台门禁（见「陷阱」）
     ├─ req.extra_user_content_parts.append(TextPart(text))
     └─ req.image_urls.extend(images)

③ AstrBot 内部
   assemble_context() → 组装成一条 user 消息 → _save_to_history() → 会话历史
```

`fetch_post()` 内部的取数优先级：

```
fetch_post(creds, share_url)
  │
  ├─ _resolve_share()                  跟随 302 跳转，拿最终地址与参数
  │
  ├─ 首选：_fetch_from_share_page()    解析 h5 分享页
  │     ├─ _share_page_candidates()    候选地址（带/不带尾斜杠）
  │     ├─ _get_html()                 抓页面并试 utf-8 / gbk
  │     ├─ frontpage.extract_share_post()
  │     └─ _post_from_cell()           转成 QzonePost（含 cell_original）
  │
  └─ 兜底：_fetch_via_msglist()        列表接口
        ├─ _extract_host_uin()         只认明确字段，认不出就放弃
        ├─ _fetch_msglist()            emotion_cgi_msglist_v6
        ├─ _pick_feed()                定位；定位不到返回 None
        └─ _fetch_detail()             取全文
```

## API 参考

### `main.py`

| 成员 | 说明 |
| --- | --- |
| `QzoneReaderPlugin.capture_qzone_share(event)` | `@filter.event_message_type(ALL)`。识别链接并预取内容，结果存入 `_pending` |
| `QzoneReaderPlugin.inject_qzone_content(event, req)` | `@filter.on_llm_request()`。注入文本与图片 |
| `_build_payload(event, share_url) -> (str, list[str])` | 取内容 + 渲染 + 决定附图，返回待注入文本与图片 URL |
| `_fetch_with_retry(event, creds, url)` | 登录态失效时清缓存重取一次 |
| `_get_credentials(event)` | 按 `cookie_source` 取登录态，带 TTL 缓存与手动兜底 |
| `_credentials_from_protocol(event)` | 调 `get_cookies`，依次试 `user.qzone.qq.com`、`qzone.qq.com` |
| `_call_get_cookies(client, domain)` | 兼容 `client.call_action` 与 `client.api.call_action` 两种挂载 |
| `_get_bot(event)` | 取协议端实例，`event.bot` 优先，回退 `context.get_platform_inst()` |
| `_platform_ok(event)` | 手动平台门禁，只放行 `aiocqhttp` |
| `_find_share_url(event)` | 从消息链找分享链接（含被引用消息） |
| `_scan_chain(chain, depth)` | **递归**扫描消息链，返回 `(链接, 降级文案片段)`；处理 `Reply.chain` |
| `_card_text(event)` | 复用 `_scan_chain` 取降级文案 |
| `_in_scope(event)` | 会话白名单判断 |
| `_summarize_mode()` | 解析 `summarize_mode`，返回 `off` / `brief` / `full`；兼容旧键 `auto_summarize`，非法值回退 `brief` |
| `_keep_in_context()` | 解析 `context_mode`，`keep` 返回 `True`，`once` 返回 `False` |
| `_with_instruction(body, mode)` | 按模式决定是否给正文加指令前缀 |
| `_append_extra_part(req, text, persist)` | 注入文本块；`persist=False` 时标记 `mark_as_temp()` |
| `_append_image_part(req, url)` | 以**临时** `ImageURLPart` 注入图片，避免落历史 |
| `_append_extra_part(req, text)` | 注入文本块，优先 `TextPart`，取不到退回 dict |

模块级常量：

| 常量 | 说明 |
| --- | --- |
| `BRIEF_SUMMARY_INSTRUCTION` | `summarize_mode=brief` 的指令：要点式总结后自然接话 |
| `FULL_SUMMARY_INSTRUCTION` | `summarize_mode=full` 的指令：以完整总结为主体，6 条要点清单 |
| `FAILURE_HINT` | 读取失败时给模型的提示，明确要求「不要编造」 |

> **图片数量没有硬上限。** 早期版本有一个值为 `9` 的图片硬上限常量（现已删除），
> 导致用户把 `max_images` 调到 14 却只发 9 张、且没有任何提示。现在 `max_images`
> 是唯一上限，切片也计入其中。`test_core.py` 的 `[17]` 段有一条断言守着这个常量不会被加回来。

### 总结模式与指令

`summarize_mode` 是唯一影响「bot 怎么回应」的配置，三档对应三种指令注入策略：

| 值 | `_with_instruction` 行为 |
| --- | --- |
| `off` | 原样返回正文，不加任何前缀 |
| `brief` | 前缀 `BRIEF_SUMMARY_INSTRUCTION` |
| `full` | 前缀 `FULL_SUMMARY_INSTRUCTION` |

改动这两个指令常量时注意：

- `full` 的第 6 条（要求指出信息不全、不要替作者补全）是**刻意加的约束**。瓜条类内容常是单方说法，去掉这条会让 bot 顺着原文情绪跑偏。
- 三条路径产出的内容**都会进历史**（因为都作为 `extra_user_content_parts` 注入），改指令不会影响持久化。
- `test_core.py` 的 `[9]` 段断言了 `full` 必须包含「完整地总结」「来龙去脉」「作者的情绪」「配图」「信息不完整」等关键词，改指令时同步改测试，否则会红。

### `core/qzone_api.py`

数据模型：

| 类型 | 说明 |
| --- | --- |
| `QzoneCredentials` | `uin` / `skey` / `p_skey` / `source`；`gtk` 属性推导 `g_tk`；`headers()` 生成请求头 |
| `QzonePost` | 一条说说的可读内容，转发用 `original_*` 字段表达第二层 |
| `QzoneAuthError` | 登录态失效。调用方据此清缓存重取 |
| `CookieCache` | 带 TTL 的登录态缓存，`ttl=0` 表示不缓存 |

`QzonePost` 字段：

| 字段 | 含义 |
| --- | --- |
| `uin` / `name` / `created_time` | 外层（转发场景下即转发者） |
| `text` | 外层正文；转发场景下是转发语 |
| `rt_text` | 引用内容（外层显式引用时才有） |
| `images` / `videos` | 外层配图与视频 |
| `original_name` / `original_uin` / `original_time` | 被转发的原文元信息 |
| `original_text` | 被转发的原文正文 |
| `original_images` | 被转发的原文配图（外层的图会并入这里） |
| `url` | 最终分享页地址 |

方法：`is_repost()`、`is_empty()`、`to_prompt(max_images=0)`。

链接与卡片：

| 函数 | 说明 |
| --- | --- |
| `extract_share_url(payload)` | 递归扫描任意 JSON/文本，找出 QQ空间分享地址。会先 `unescape`，因为 QQ 卡片把逗号转义成 `&#44;` |
| `extract_card_text(payload, limit=300)` | 取卡片标题/摘要作为降级文案 |
| `parse_share_url(url)` | 解析 query。分隔符同时支持 `&` 和 `,` |
| `credentials_from_cookie_string(s, source)` | 解析 Cookie，缺 `uin`/`skey` 返回 `None` |
| `_extract_host_uin(params)` | 只认 `res_uin`/`host_uin`/`uin` 且校验位数 |
| `_has_feed_locator(params)` | 判断一组参数能否定位到具体说说 |
| `_looks_like_share(url)` | 是否是说说分享链接 |

取数：

| 函数 | 说明 |
| --- | --- |
| `fetch_post(creds, url, timeout=20)` | 总入口，分享页优先、列表接口兜底 |
| `_resolve_share(session, creds, url)` | 跟随跳转拿最终地址；失败则从 HTML 里找 |
| `_fetch_from_share_page(...)` | 抓分享页并解析 |
| `_share_page_candidates(url)` | 生成候选地址 |
| `_post_from_cell(cell, url)` | cell → `QzonePost`，处理转发两层 |
| `_fetch_via_msglist(...)` | 列表接口路径 |
| `_fetch_msglist(...)` / `_fetch_detail(...)` | 两个 cgi 接口，返回 `(数据, 错误码)` |
| `_pick_feed(msglist, cell_id, share_url)` | 定位说说；**定位不到返回 `None`，绝不猜** |
| `_post_from_feed(feed, share_url, base)` | feed → `QzonePost` |
| `_get_html` / `_get_text` / `_loads_jsonp` | 传输层，含编码兜底与 jsonp 解包 |

### `core/frontpage.py`

纯函数，无网络无 AstrBot 依赖。

| 函数 | 说明 |
| --- | --- |
| `extract_share_post(html)` | 从 HTML 解出说说 cell 字典，失败返回 `None` |
| `cell_text(cell)` | 正文 |
| `cell_time(cell)` | 发布时间 |
| `cell_author(cell)` | `(uin, 昵称)` |
| `cell_images(cell, limit=0)` | 配图，自动挑最大尺寸 |
| `cell_video(cell)` | 视频地址 |
| `cell_original(cell)` | 被转发的原文 cell |
| `is_repost(cell)` | 是否转发 |

内部解析器（`_` 前缀为私有，但测试会直接用）：

- `_frontpage_object_starts(source)` — 找 `FrontPage = {` 的位置，排除 `xxxFrontPage` 这类更长标识符
- `_balanced_object(source, start)` — 按括号配平切出对象，正确跳过字符串转义与 `//`、`/* */` 注释
- `_extract_top_level_value(outer, key)` — 在对象第一层取 `key: {...}`

### `core/images.py`

唯一依赖 Pillow 的模块，且延迟导入。

| 函数 | 说明 |
| --- | --- |
| `is_tall(width, height, threshold)` | 是否算长截图：`height >= threshold` 且 `height > width * 1.5` |
| `slice_image(data, slice_height, overlap, max_slices)` | 切成长图 data URL 列表；不需切或失败返回 `[]` |
| `fetch_bytes(session, url, headers, timeout)` | 下载图片字节，失败返回 `None` |
| `prepare_images(session, urls, budget, ...)` | 按预算挑选图片，长图切片、其余原样 |

常量：`DEFAULT_TALL_THRESHOLD=1600`、`DEFAULT_SLICE_HEIGHT=1280`、`DEFAULT_OVERLAP=80`、`MAX_SLICES_PER_IMAGE=12`、`JPEG_QUALITY=88`。

设计要点：

- **保持宽度不变**。文字清晰度取决于宽度，等比缩放才是导致长截图糊掉的元凶，所以只切高度。
- **片间留 `DEFAULT_OVERLAP=80` 像素重叠**，避免把一条聊天消息拦腰截断。
- 切片输出为 **data URL**（`data:image/jpeg;base64,...`）。已验证 AstrBot 的 `MediaResolver` 支持 data URI（`media_utils.py` 的 `startswith("data:")` 分支），因此不必二次下载，也自带内容不依赖网络。
- `PIL` 的 CPU 操作走 `asyncio.to_thread()`，不阻塞事件循环。
- 任何一步失败都**退回原 URL 而不是丢弃图片**，保证降级路径不会让内容变少。
- `budget`（即 `max_images`）是**最终张数上限**，切片计入其中。这是防止一本 14 张长图的瓜条被切成 50+ 片的关键。

## FrontPage 数据结构

h5 分享页里有一段 JS 赋值，结构如下：

```javascript
var FrontPage = {
  loginUin : 'NaN',
  module : "detail",
  data : { ret:0, code:0, message:"", data: { /* 真正的说说数据 */ } }
};
```

`data.data` 的关键字段：

| 字段 | 内容 |
| --- | --- |
| `cell_comm` | `time` 发布时间、`appid`、`ugckey`（格式 `<uin>_<appid>_<cellid>_`）、`curlikekey`、`orglikekey` |
| `cell_userinfo.user` | `uin`、`nickname` |
| `cell_summary.summary` | **正文** |
| `cell_pic.picdata[]` | 配图数组，见下 |
| `cell_original` | **被转发的原文**，结构与外层同构（可递归） |
| `cell_comment` / `cell_like` | 评论与点赞 |
| `cell_remark` | 如「共 15张照片」 |

`picdata[]` 每一项：

| 字段 | 内容 |
| --- | --- |
| `photourl` | 多档尺寸的对象，键为 `"0"`/`"1"`/`"11"` 等，值含 `url`/`width`/`height` |
| `raw` / `sloc` | 原始地址 |
| `videoflag` / `videodata` | 视频标记与地址 |
| `lloc` | 内部定位串（不是 URL） |

**挑图策略**：优先 `photourl` 里 `width*height` 最大的；URL 含 `/o&`、`/b&`、`origin`、`raw`、`large` 等原图特征再加权；键为 `"0"` 的加一点权重。都没有才退回 `raw`/`sloc`。

### 转发的判定

`cell_original` 存在即为转发。分享页在 `cell_comm` 里也给了线索：

```
curlikekey : http://user.qzone.qq.com/<转发者uin>/mood/<当前cellid>
orglikekey : http://user.qzone.qq.com/<原作者uin>/mood/<原cellid>
```

超长瓜条会触发一个坑：当正文超过一定长度时，QQ空间会把正文挪到 `cell_original` 里，
外层正文变成「本文转自…」之类的引导语。所以**判断转发不能只看 `cell_original` 是否存在**。

当前实现没有专门处理这个情形：一律把 `cell_original` 的内容当原文渲染。
结果是外层显示引导语、原文区块显示真正的长正文 —— **内容不丢，只是标签略有偏差**。

如果要精确区分「用户真的转了别人的说说」和「正文太长被折叠」，
`cell_comm.actiontype` 是一个候选判据（实测转发说说与原文的该字段取值不同），
但具体取值语义尚未确认，改之前请先抓真实响应核对，不要凭猜测下判据。

## 关键设计决策

### 为什么改走分享页而不是列表接口

最初用 `emotion_cgi_msglist_v6`，实测必然失败：

- 分享短链解析后的 `res_uin` 是 **uid 不是 QQ 号**，拿它查列表查的是别人；
- `cellid` 是 **h5 分享页的标识**，和列表接口返回的 cellid 不是同一套，永远匹配不上；
- 即使匹配上，转发内容在 `rt_con` 里只有一个 `content` 字段，**原文图片完全没有**。

分享页的 `FrontPage` 数据里 `cell_original` 是完整结构，正文、配图、作者、时间都齐，这才是正确来源。

### 为什么 `_pick_feed` 不用 `msglist[0]` 兜底

**这是踩过的坑。** 早期实现在 `cellid` 匹配不上时用 `msglist[0]` 兜底，导致：

> 用户转发 A 的说说，插件查到的是机器人自己空间的动态，
> 然后随手取第一条，把**完全无关的说说**注入给了用户。

宁可如实返回 `None` 走降级，也不能猜。`_extract_host_uin` 同样：认不出就放弃，**绝不退回自己的 uin**。`test_core.py` 的 `[8c]` 段专门钉死这两点。

### 为什么登录态要带 TTL 缓存

`get_cookies` 是 OAuth 调用，每次消息都问一遍协议端没有必要。默认缓存 600 秒。失效时从两条路发现：接口返回 `AUTH_ERROR_CODES`（抛 `QzoneAuthError`），或请求异常。两种情况都会清缓存重取一次。

### 为什么内容要提前取

`on_llm_request` 里做网络请求会阻塞 LLM 请求链路（AstrBot 在那里持有 session lock）。所以在消息阶段取好，暂存在 `self._pending[id(event)]`，注入时 `pop` 掉。

`id(event)` 作键在注入后会立刻清理，正常情况下不会泄漏；极端情况下某条消息没走到 LLM 请求，会残留一条，属于可接受的小泄漏。

### 为什么附图优先原文配图

转发场景下原文图才是主体（转发者往往不配图）。`_build_payload` 按 `[原文图, 外层图]` 顺序取，外层额度用完就停。

### `context_mode` 的实现机制与边界

AstrBot 的持久化开关只有 `ContentPart.mark_as_temp()`（置 `_no_save=True`），
落历史时由 `dump_messages_with_checkpoints()` 过滤：

```python
# core/agent/message.py:350-355
message_data["content"] = [
    part.model_dump()
    for part in message.content
    if not getattr(part, "_no_save", False)   # ← 临时 part 在这里被丢掉
]
```

据此 `context_mode` 分两条路：

| 模式 | 文本 | 图片 |
| --- | --- | --- |
| `keep` | 普通 `TextPart` → 落库 | 追加到 `req.image_urls` → 落库（base64） |
| `once` | `TextPart.mark_as_temp()` | 临时 `ImageURLPart` 塞进 `extra_user_content_parts` |

**图片为什么不能走 `req.image_urls`**：`assemble_context()` 把 `image_urls`
转成 `image_url` 内容块时**不带 `_no_save`**（`provider/entities.py:251-273`），
所以一旦进了那个字段就必然落库。要让它不持久化，只能改走
`extra_user_content_parts` 里的临时 `ImageURLPart` ——
`assemble_context()` 同样会解析该字段里的图片（`entities.py:220-248`），
所以对模型的效果一样，但能被过滤掉。

`ImageURLPart` 的构造参数**必须是 dict**（`{'url': ...}`），传字符串会
`ValidationError`：

```python
ImageURLPart(image_url={'url': url})          # ✅
ImageURLPart(image_url=url)                   # ❌ ValidationError
```

三条无法绕过的边界，已写进 README：

1. **用户那条卡片消息由 AstrBot 自己入库**（`persist_group_message` / 会话管理器），
   插件无法阻止它落库，`once` 模式下它会以空壳卡片形式留在历史里。
2. **bot 的回复照常落库**，`context_mode` 只管说说内容。
3. `Message._no_save` 能整条消息不落库，但那是 `Message` 层级的属性，
   而用户消息不是插件构造的，改不到。

### 引用消息必须递归 `Reply.chain`

群聊里「先发卡片、再引用它并 @bot」是高频用法，而被引用的内容**不在顶层消息链上**，
只存在于 `Reply.chain` 里：

```python
# aiocqhttp_platform_adapter.py:303-339（get_reply 默认为 True）
reply_event_data = await self.bot.call_action("get_msg", message_id=...)
abm_reply = await self._convert_handle_message_event(new_event, get_reply=False)
reply_seg = Reply(id=..., chain=abm_reply.message, ...)   # ← 卡片在这里
abm.message.append(reply_seg)
```

所以 `_scan_chain()` 必须递归进 `Reply.chain`，否则群聊引用场景完全失效。
两个实现细节：

1. **`depth > 5` 保护**：`Reply.chain` 理论上可自引用，测试里专门构造了一个
   自引用对象验证不会无限递归。
2. **命中链接后不能提前返回**。早期版本一找到 URL 就 `return`，导致同一消息里
   链接**之后**的文字被整段丢掉 —— 而 `@bot 这瓜啥情况` 正好在引用段之后。
   正确做法是扫完整条链，把链接和文案分别收集。

`_card_text()` 也复用 `_scan_chain()`，保证「能认出卡片」与「能取到降级文案」
的范围一致，不出现单边失效。

## 两个必须知道的 AstrBot 陷阱

这两个都是实际踩到的，改动 `main.py` 时务必保持现状。

### 1. `event_message_type` / `platform_adapter_type` 在 `on_llm_request` 上无效

它们注册进 `md.event_filters`，而 `event_filters` **只在 AdapterMessageEvent 路径上被求值**，`call_event_hook` 根本不读。所以：

```python
@filter.on_llm_request()
async def inject_qzone_content(self, event, req):
    ...
    if not self._platform_ok(event):   # ← 必须手动门禁
        return
```

给 `on_llm_request` 加 `@filter.platform_adapter_type(...)` 是**静默失效**的，不会报错，只会让其他平台也吃到注入。

### 2. `on_llm_request` 里不能调 `stop_event()`

`internal.py` 里 `call_event_hook(OnLLMRequestEvent, req)` 返回真值就直接 `return`，runner 不启动；而 `_save_to_history` 又被 `not event.is_stopped()` 把着。结果是**本轮消息完全不落历史**，注入的内容也就丢了。

正确做法：注入后正常返回。

若确实想阻止默认的 LLM 请求，用 `event.should_call_llm(True)` —— 注意这个方法名字反直觉，它设置的是 `event.call_llm` 标志，而消费处写的是 `not event.call_llm`：

```python
# core/pipeline/process_stage/stage.py:56-60
if (not event._has_send_oper
        and event.is_at_or_wake_command
        and not event.call_llm):
    ...  # 走默认 LLM 链路
```

所以 `should_call_llm(True)` 是**跳过**默认 LLM 请求，`should_call_llm(False)` 才是允许。它只影响 AstrBot 默认链路，不影响插件自己发起的 LLM 请求。

### 另外两点

- `Comp.Json.data` 已经是 `json.loads` 过的 dict，不要再解一次。
- 适配器**没有** `Ark`/`LightApp` 组件，`"ark"` 不在 `ComponentTypes` 里，这类消息段会被丢弃并打警告 —— 所以 `_find_share_url` 留了 `raw_message` 兜底。

## 测试

`test_core.py` 是**自包含**的：不装 AstrBot、不联网，用桩模块注入 `astrbot.api` / `aiohttp` 后加载真实插件代码。

```bash
cd astrbot_plugin_qzone_reader
python test_core.py
```

当前 **180 项断言**。分段：

| 段 | 覆盖 |
| --- | --- |
| `[1]`–`[3]` | 卡片链接提取、参数解析、Cookie 与 `g_tk` |
| `[4]`–`[6]` | jsonp 解析、feed 字段映射、文本渲染 |
| `[7]` | Cookie 缓存 TTL |
| `[8]` `[8b]` `[8c]` | 说说定位、短链参数识别、**认不出时必须放弃**（安全回归） |
| `[8d]` | **转发：必须读到原文**，且不重复渲染 |
| `[9]`–`[13]` | 插件组装、白名单、卡片识别、转义还原、平台门禁 |
| `[14]`–`[15]` | 登录态失效检测与自动重取 |
| `[16]` | **长截图切片**：尺寸判定、切片尺寸、预算约束、降级路径 |
| `[17]` | 配置项接线（含「已移除硬上限」的断言） |
| `[18]` | **上下文保留**：两种模式的临时标记、落历史过滤、非法值回退 |
| `[19]` | 注入流程按 `context_mode` 分流（图片走哪个字段） |
| `[20]` | **群聊引用卡片**：`Reply.chain` 递归、嵌套引用、自引用保护、顶层优先 |

> **写测试桩时注意**：`fetch_bytes` 和 `_get_html` 都会读 `resp.status`，桩必须提供该属性。早期漏了它，导致 `status >= 400` 抛 `AttributeError` 被兜底 `except` 吞掉，表现为「图片莫名退回原 URL」——排查了好一阵。

### fixture

`tests/fixtures/repost_cell.json` 是一条真实转发说说的**脱敏**数据（昵称已抹掉，QQ 号保留，结构与线上一致）。它用于 `[8d]` 段，覆盖分享页解析 → cell 转换 → 渲染的完整链路。

`test_core.py` 会用 `json.dumps` 把它包成 JS 对象字面量的形式，以复刻真实页面：

```python
html = (
    "<html><script>var FrontPage = {\n"
    " loginUin : 'NaN', // 页面注释\n"
    ' module : "detail",\n'
    f" data : {cell_json}\n"
    "};</script></html>"
)
```

改动 `frontpage.py` 或 `_post_from_cell` 时，务必保证 `[8d]` 全绿。

## 调试

日志前缀统一 `[qzone_reader]`：

```bash
grep qzone_reader /AstrBot/data/logs/astrbot.log
```

关键日志与含义：

| 日志 | 含义 |
| --- | --- |
| `检测到 QQ空间分享，开始读取: <url>` | 链接识别成功 |
| `使用 <source> 提供的 QQ空间登录态` | 登录态就位 |
| `分享短链已解析为: <url>` | 302 解析成功 |
| `这是一条转发：原作者 X（QQ N），已连同原文一起读取` | 转发两层都拿到 |
| `分享页没取到内容，改走动态列表接口兜底` | 分享页无数据 |
| `在 uin=X 的动态里没找到该条说说（cellid=Y），放弃` | 列表接口也定位不到 |
| `分享链接里没有可用的说说定位参数，放弃读取` | 参数不足，安全放弃 |
| `没有可用的 QQ空间登录态` | 登录态获取失败 |

### 排查分享页解析

遇到解析问题时，保存一份真实响应再离线调试：

```bash
curl -sL -o share.html \
  -A 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36' \
  'https://h5.qzone.qq.com/ugc/share/?<参数>'
```

```python
import pathlib
from core import frontpage
html = pathlib.Path("share.html").read_text(encoding="utf-8", errors="replace")
cell = frontpage.extract_share_post(html)
print(cell.keys() if cell else "解析失败")
```

解析失败时按顺序检查：`FrontPage` 字样在不在 → `_balanced_object` 有没有切错 → `data` 字段有没有取到 → 是不是被重定向到登录页。

## 扩展指引

### 支持新的分享链接形态

1. 在 `_looks_like_share()` 加上 URL 特征；
2. 若该形态需要额外解析步骤，在 `_resolve_share()` 里补；
3. 在 `_share_page_candidates()` 加候选地址；
4. 加对应的 `_extract_host_uin` 字段（如果有新的 uin 字段名）；
5. 在 `test_core.py` `[1]`/`[2]`/`[8b]` 段补断言。

### 支持新的内容字段（如长文、音乐、位置）

1. 在 `frontpage.py` 加 `cell_xxx()` 纯函数；
2. 在 `_post_from_cell()` 里填进 `QzonePost`；
3. 在 `QzonePost.to_prompt()` 里渲染；
4. 补测试。

`to_prompt()` 是唯一面向模型的渲染出口，改动时注意别破坏转发两层的结构 —— `[8d]` 里有一条「原文正文不重复出现」的断言，就是防这个的。

### 加配置项

三处都要改，缺一个就会出现「配置里有但代码不读」的假开关：

1. `_conf_schema.json` 加字段；
2. `main.py` 里 `self.config.get(...)` 读取；
3. 需要的话补 README 配置表。

## 已知脆弱点

| 位置 | 风险 |
| --- | --- |
| `frontpage.py` 的括号配平解析 | 腾讯改分享页结构或改用标准 JSON 时会失效。换成 `json.loads` 是升级方向 |
| `photourl` 的尺寸键 | `"0"`/`"1"`/`"11"` 等键含义未证实，现按面积挑最大，属于启发式 |
| `_pick_feed` 的时间戳兜底 | 分享页路径失效、退回列表接口时才用到，窗口 ±120 秒，可能误匹配 |
| `AUTH_ERROR_CODES` | 只收了 `-3000`/`-10000`/`-4001`，其他失效码会走普通异常路径（不重取登录态） |
| `id(event)` 作暂存键 | 极端情况下未走到 LLM 请求的消息会残留一条暂存 |
| 分享页可能返回登录页 | 此时 `FrontPage` 仍在但 `data` 为空，会落到列表接口兜底 |
| 视频 | 只记录数量，未解析。`cell_video()` 已能取地址，但未接入注入 |
| 切片参数为经验值 | `DEFAULT_SLICE_HEIGHT=1280`、`is_tall` 的 1.5 倍判据都基于常见视觉模型的缩放行为，未针对具体模型实测校准。不同模型的上限不同，必要时可用 `slice_max_height` 调整 |
| 切片不计内容边界 | 按固定像素切，重叠 80px 只能降低切断风险，无法保证不切断一条消息 |
