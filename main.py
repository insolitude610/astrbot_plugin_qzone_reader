"""把转发过来的 QQ空间说说读进对话上下文。

流程：识别转发卡片 -> 用 NapCat 登录态拉取说说全文 -> 在 on_llm_request 里
把内容作为额外用户内容块注入。由于 AstrBot 会把「prompt + 额外内容块 + 图片」
组装成同一条 user 消息并写入会话历史，注入的内容天然进入上下文，
之后可以继续追问讨论。
"""

from __future__ import annotations

import uuid
from time import monotonic

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Json, Plain, Reply

import aiohttp

from .core import images as image_utils
from .core.qzone_api import (
    DEFAULT_COOKIE_TTL,
    CookieCache,
    QzoneAuthError,
    QzoneCredentials,
    credentials_from_cookie_string,
    extract_card_text,
    extract_share_url,
    fetch_post,
    fetch_trusted,
)

# 待注入内容的暂存键与存活时间。
# 内容本身留在插件里，事件上只挂一个 token —— 图片可能是好几 MB 的 base64，
# 不该塞进 event._extras（那是个公共字典，别人 get_extra() 一把捞出来会很难看）。
PENDING_EXTRA_KEY = "qzone_reader_pending"
PENDING_TTL = 300

# 统一保留：一次注入里的图片总数上限由 max_images 决定，不再有隐藏硬上限。
# 两种模式共用的叙述规范，放在指令最前面。
# 这两条是被实际输出教育出来的：
#   1. 只说「用你平时说话的口吻」，模型会理解成「用说话人的口吻」，
#      整篇变成作者第一人称自述（「你说我…」「我 3 月 6 日进群…」），
#      读者分不清哪句是作者说的、哪句是 bot 的判断。
#   2. 只说「完整地总结」，模型倾向把原文句子改写一遍，
#      读起来是浓缩版原文，而不是归纳后的结论。
NARRATION_RULES = (
    "【叙述规范，务必遵守】\n"
    "A. 全篇用第三人称叙述。总结部分要写「谁主张什么、谁指控什么」，"
    "可以用「作者称」「他指出」「原文提到」这类转述，"
    "但不要用第一人称代入任何一方 —— 不要写「我 3 月 6 日进群」这种句子，"
    "要写成「作者称他 3 月 6 日进群」。只有最外层开场和结尾可以用你自己的口吻。\n"
    "B. 这是归纳总结，不是把原文缩写一遍。要按条理用自己的话重述要点，"
    "把散落在原文各处的信息归到对应类别下；不要逐句搬运或改写原句。\n"
)

# summarize_mode = brief：要点式总结后自然接话（默认，最省）
BRIEF_SUMMARY_INSTRUCTION = (
    "用户转发了一条 QQ空间说说，上面的【QQ空间说说原文】就是该说说的内容。\n"
    + NARRATION_RULES
    + "请先简要总结这条说说讲了什么（作者的表达、情绪或重点），"
    "然后用自然的口吻回应；用户接下来可能会继续追问或和你讨论这条说说。"
)

# summarize_mode = full：以总结为主体，完整讲清楚
FULL_SUMMARY_INSTRUCTION = (
    "用户转发了一条 QQ空间说说，上面的【QQ空间说说原文】就是该说说的内容。\n"
    + NARRATION_RULES
    + "请完整地总结这条说说，但内容要讲全：\n"
    "1. 谁说给谁听的、发布与转发的时间；\n"
    "2. 事情的来龙去脉，按条理把要点讲清楚；\n"
    "3. 涉及哪些人或方，各自的主张与立场；\n"
    "4. 作者的情绪与意图；\n"
    "5. 【正文与配图的对应】这是重点，不要只笼统说「配图是聊天记录」。\n"
    "   正文若用 p1、p3-6 这类编号标注了配图，请沿用原文的编号，\n"
    "   逐条说明「正文的哪句话 / 第几点」对应「第几张、第几张图」，"
    "以及那几张图里具体是什么内容（谁和谁的对话、什么截图、能看出什么）。\n"
    "   正文没有明确编号时，按你看到的图片顺序自行编号说明。\n"
    "6. 信息不完整或属于单方说法的地方，明确指出来，不要替作者补全。\n"
    "总结之后可以简短说一句你的看法，但总结本身要完整。"
)

FAILURE_HINT = (
    "（用户转发了一条 QQ空间说说卡片，但插件没能读取到内容，"
    "可能是登录态失效或该说说的可见范围受限。"
    "请告知用户读取失败，不要编造说说内容。）"
)


class QzoneReaderPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.cookies = CookieCache(
            self.config.get("cookie_ttl", DEFAULT_COOKIE_TTL)
        )
        # 每一条待注入的内容用一个随机 token 索引，token 挂在 event 上。
        # 不用 id(event) 作键：事件对象回收后 id 会被复用，残留的旧条目可能被
        # 后来的无关消息 pop 掉，把别人的说说内容注入进这一轮对话。
        self._pending: dict[str, tuple[str, list[str], float]] = {}

    # ------------------------------------------------------------------ 读取

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def capture_qzone_share(self, event: AstrMessageEvent):
        """识别 QQ空间分享卡片，先把正文和图片准备好，等注入阶段使用。"""
        self._sweep_pending()
        try:
            await self._capture(event)
        except Exception as exc:  # noqa: BLE001
            # 这里绝不能抛出去：本 handler 跑在 AstrBot 的插件 handler 循环里，
            # 抛异常会被 stop_event，本轮连 LLM 都不会请求，还会给用户回一句报错。
            logger.warning("[qzone_reader] 处理分享卡片时异常，已忽略本轮: %s", exc)

    async def _capture(self, event: AstrMessageEvent) -> None:
        if not self._in_scope(event):
            return

        # 没被唤醒就完全不干活。
        # 群里发一张卡片但没 @bot 时，AstrBot 的唤醒检查会拦住 LLM 请求
        # （process_stage/stage.py:58 读的就是这个标志），bot 不会回复；
        # 但插件原本照样抓页面、下载十几张长图、切片几十张，全部白做。
        # 而这一步恰恰是整个插件最重的操作，必须提前挡掉。
        if not self._will_wake_bot(event):
            return

        share_url = self._find_share_url(event)
        if not share_url:
            return

        logger.info("[qzone_reader] 检测到 QQ空间分享，开始读取: %s", share_url)

        text, images = await self._build_payload(event, share_url)
        if not text:
            return
        token = uuid.uuid4().hex
        self._pending[token] = (text, images, monotonic())
        if not self._remember_token(event, token):
            # 事件上挂不住 token 时，保留条目只会变成无人认领的垃圾
            self._pending.pop(token, None)

    # ------------------------------------------------------------ 待注入暂存

    @staticmethod
    def _remember_token(event: AstrMessageEvent, token: str) -> bool:
        """把 token 挂到事件上，供注入阶段取回。

        取不到 set_extra（测试桩或极老的适配器）时返回 False，调用方会丢弃该条目。
        """
        setter = getattr(event, "set_extra", None)
        if not callable(setter):
            return False
        try:
            setter(PENDING_EXTRA_KEY, token)
        except Exception:  # noqa: BLE001
            return False
        return True

    @staticmethod
    def _read_token(event: AstrMessageEvent) -> str | None:
        """从事件上取回 token；取不到一律当作「没有待注入内容」。"""
        getter = getattr(event, "get_extra", None)
        if not callable(getter):
            return None
        try:
            token = getter(PENDING_EXTRA_KEY)
        except Exception:  # noqa: BLE001
            return None
        return token if isinstance(token, str) and token else None

    def _sweep_pending(self) -> None:
        """清掉过期的暂存条目。

        正常路径下注入阶段会 pop 掉；但如果某条消息最终没走到 LLM 请求
        （比如被别的插件 stop_event 了），条目就会留下来，这里兜底回收。
        """
        if not self._pending:
            return
        now = monotonic()
        stale = [key for key, item in self._pending.items() if now - item[2] > PENDING_TTL]
        for key in stale:
            self._pending.pop(key, None)

    def _take_pending(self, event: AstrMessageEvent) -> tuple[str, list[str]] | None:
        """取出并清理本轮待注入内容；没有则返回 None。"""
        token = self._read_token(event)
        if token is None:
            return None
        entry = self._pending.pop(token, None)
        if entry is None:
            return None
        text, images, created_at = entry
        if monotonic() - created_at > PENDING_TTL:
            return None
        return text, images

    @staticmethod
    def _will_wake_bot(event: AstrMessageEvent) -> bool:
        """这条消息是否真的会唤醒 bot（决定本轮有没有 LLM 请求）。

        取 AstrBot 自己的标志位，不自己猜唤醒规则：
        `waking_check` 阶段命中 @bot / 唤醒前缀 / 私聊直达时才置 True，
        默认 False；`process_stage` 正是用它决定要不要走 LLM 链路。

        取不到该属性时保守放行 —— 宁可多干活，也不能漏掉本该处理的分享。
        """
        value = getattr(event, "is_at_or_wake_command", None)
        if value is None:
            return True
        return bool(value)

    @filter.on_llm_request()
    async def inject_qzone_content(self, event: AstrMessageEvent, req: ProviderRequest):
        """把说说内容作为额外用户内容块注入，从而进入本轮对话并落进历史。

        注意：on_llm_request 不经过 event_filters，平台/会话过滤必须在这里手动做。
        另外这里绝不能调用 stop_event()，否则本轮消息不会被写入历史。
        """
        self._sweep_pending()
        payload = self._take_pending(event)
        if payload is None:
            return
        if not self._platform_ok(event):
            return
        text, images = payload
        keep = self._keep_in_context()

        try:
            self._append_extra_part(req, text, persist=keep)
            for url in images:
                if keep:
                    # 走 image_urls；AstrBot 会把图一并写入历史
                    req.image_urls.append(url)
                else:
                    # 走临时图片 part；落历史时会被过滤掉，图片不会残留
                    self._append_image_part(req, url)
        except Exception as exc:  # noqa: BLE001 - 注入失败不应打断对话
            logger.warning("[qzone_reader] 注入说说内容失败: %s", exc)

    # ------------------------------------------------------------------ 组装

    async def _build_payload(
        self, event: AstrMessageEvent, share_url: str
    ) -> tuple[str, list[str]]:
        """拉取说说内容并渲染成待注入的文本与图片列表。"""
        max_images = self._bounded_int("max_images", 4)
        mode = self._summarize_mode()
        notify = bool(self.config.get("notify_on_failure", True))

        post = None
        creds: QzoneCredentials | None = None
        if self.config.get("read_full_feed", True):
            creds = await self._get_credentials(event)
            if creds is not None:
                post = await self._fetch_with_retry(event, creds, share_url)
            else:
                logger.warning("[qzone_reader] 没有可用的 QQ空间登录态")

        if post is None or post.is_empty():
            # 读不到正文时至少把卡片上已有的文字交给模型
            fallback = self._card_text(event)
            if fallback:
                if notify:
                    body = (
                        "【QQ空间说说原文（只拿到转发卡片上的文字，"
                        "未能读取到说说正文，内容可能不完整）】\n"
                        f"{fallback}"
                    )
                else:
                    # 明确要求别声张，避免每张卡片都回一句"没读到"
                    body = f"{fallback}\n\n（请直接基于上面的文字回应，不要提及读取失败。）"
                return self._with_instruction(body, mode), []
            if not notify:
                return "", []
            return FAILURE_HINT, []

        # 转发场景下原文配图才是主体，优先附上，剩余额度再给外层配图
        candidates: list[str] = []
        for url in [*post.original_images, *post.images]:
            if url not in candidates:
                candidates.append(url)

        images, covered = await self._prepare_images(
            event, candidates, max_images, creds
        )

        body = post.to_prompt(max_images=len(images), covered_images=covered)
        return self._with_instruction(body, mode), images

    async def _prepare_images(
        self,
        event: AstrMessageEvent,
        candidates: list[str],
        budget: int,
        creds: QzoneCredentials | None,
    ) -> tuple[list[str], int]:
        """按预算准备配图：长截图切片、超宽图缩放。

        budget 是最终图片总数上限，切片计入其中 —— 否则一本瓜条的十余张
        长图能切成几十片，token 会失控。

        Returns:
            (图片引用列表, 实际覆盖到的候选图张数)，后者用于如实描述
            「已附带前几张原图」。
        """
        if budget <= 0 or not candidates:
            return [], 0
        slice_tall = bool(self.config.get("slice_tall_images", True))
        slice_height = self._int_config(
            "slice_max_height", 0, fallback=image_utils.DEFAULT_SLICE_HEIGHT
        )
        max_width = self._int_config(
            "image_max_width", image_utils.DEFAULT_MAX_WIDTH, allow_zero=True
        )
        quality = self._int_config("jpeg_quality", image_utils.JPEG_QUALITY)

        # 既没开切片、也不限宽度时无需任何处理
        if not slice_tall and (not max_width or max_width <= 0):
            return candidates[:budget], min(len(candidates), budget)

        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                fetcher = None
                if creds is not None:
                    # 图片下载同样走逐跳校验：凭据只发给白名单主机，
                    # 跳转目标不在白名单时不会带上 Cookie。
                    async def _fetch_image(url: str) -> bytes | None:
                        _final, status, body = await fetch_trusted(
                            session, url, creds=creds
                        )
                        if status >= 400:
                            logger.debug(
                                "[qzone_reader] 图片下载 HTTP %s: %s", status, url
                            )
                        return body or None

                    fetcher = _fetch_image

                return await image_utils.prepare_images_with_coverage(
                    session,
                    candidates,
                    budget=budget,
                    fetcher=fetcher,
                    slice_tall=slice_tall,
                    slice_height=slice_height,
                    quality=quality,
                    max_width=max_width,
                )
        except Exception as exc:  # noqa: BLE001 - 图片处理失败不该打断对话
            logger.warning("[qzone_reader] 配图处理失败，退回原始地址: %s", exc)
            return candidates[:budget], min(len(candidates), budget)

    def _bounded_int(self, key: str, default: int, *, minimum: int = 0) -> int:
        """读一个整数配置：解析失败回退 default，否则取下界。

        与旧实现（解析失败折算成 0）的区别：非法值现在回退到 schema 里的默认值，
        而不是悄悄把功能关掉 —— 例如 image_max_width 写成 "abc" 不再等于「不缩放」。
        """
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(value, minimum)

    def _int_config(self, key: str, default: int, *, fallback: int | None = None, allow_zero: bool = False) -> int:
        """读一个整数配置，非法值回退。

        fallback 用于「0 表示用默认值」的配置（如 slice_max_height）；
        allow_zero 用于「0 表示不限制」的配置（如 image_max_width）。
        """
        value = self._bounded_int(key, default)
        if value <= 0:
            if allow_zero:
                return 0
            return fallback if fallback is not None else default
        return value

    async def _fetch_with_retry(
        self, event: AstrMessageEvent, creds: QzoneCredentials, share_url: str
    ):
        """登录态失效时清缓存重取一次，仍失败则放弃（下一轮会重新获取）。"""
        try:
            return await fetch_post(creds, share_url)
        except QzoneAuthError as exc:
            logger.warning("[qzone_reader] %s，尝试重新获取登录态", exc)
        except Exception as exc:  # noqa: BLE001 - 任何异常都不该打断对话
            logger.warning("[qzone_reader] 拉取说说内容异常: %s", exc)
            return None

        self.cookies.clear()
        fresh = await self._get_credentials(event)
        if fresh is None:
            return None
        try:
            return await fetch_post(fresh, share_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[qzone_reader] 重取登录态后仍失败: %s", exc)
            return None

    def _keep_in_context(self) -> bool:
        """是否把说说内容写入会话历史。

        keep（默认）：注入的内容随本轮消息落库，之后可以继续追问。
        once：只在本次回复时给模型看，不进历史 —— 用 mark_as_temp 实现。
        """
        mode = str(self.config.get("context_mode", "keep") or "keep").strip().lower()
        return mode != "once"

    def _summarize_mode(self) -> str:
        """取总结模式：off / brief / full。

        同时兼容旧键 auto_summarize（布尔），便于已装的配置平滑迁移。
        """
        raw = self.config.get("summarize_mode")
        if raw is None:
            return "brief" if self.config.get("auto_summarize", True) else "off"
        mode = str(raw).strip().lower()
        return mode if mode in {"off", "brief", "full"} else "brief"

    @staticmethod
    def _with_instruction(body: str, mode: str) -> str:
        """按总结模式决定是否给正文加上指令前缀。"""
        if mode == "full":
            return f"{FULL_SUMMARY_INSTRUCTION}\n\n{body}"
        if mode == "brief":
            return f"{BRIEF_SUMMARY_INSTRUCTION}\n\n{body}"
        return body

    @staticmethod
    def _append_extra_part(req: ProviderRequest, text: str, *, persist: bool = True) -> None:
        """把注入文本作为额外内容块追加。

        persist=False 时标记为临时块，AstrBot 落历史时会把它过滤掉，
        因此模型本次能看到，但不会留在后续上下文里。
        """
        try:
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=text)
            if not persist:
                part = part.mark_as_temp()
        except Exception:  # noqa: BLE001
            part = {"type": "text", "text": text}
            if not persist:
                # dict 形式下用同样的约定键，落历史时会被识别
                part["_no_save"] = True
        req.extra_user_content_parts.append(part)

    @staticmethod
    def _append_image_part(req: ProviderRequest, url: str) -> None:
        """以临时图片块的形式附上一张图，避免它被写进会话历史。

        req.image_urls 里的图会被 AstrBot 一并落库（以 base64 形式），
        所以 context_mode=once 时必须走 extra_user_content_parts。
        """
        try:
            from astrbot.core.agent.message import ImageURLPart

            part = ImageURLPart(image_url={"url": url}).mark_as_temp()
        except Exception:  # noqa: BLE001
            part = {"type": "image_url", "image_url": {"url": url}, "_no_save": True}
        req.extra_user_content_parts.append(part)

    # ------------------------------------------------------------ 登录态来源

    async def _get_credentials(self, event: AstrMessageEvent) -> QzoneCredentials | None:
        cached = self.cookies.get()
        if cached is not None:
            return cached

        source = str(self.config.get("cookie_source", "napcat") or "napcat").lower()

        # 手动 Cookie 任何时候都作为兜底
        manual = credentials_from_cookie_string(
            str(self.config.get("cookies_str", "") or ""), source="manual"
        )

        creds: QzoneCredentials | None = None
        if source == "napcat":
            creds = await self._credentials_from_protocol(event)
        if creds is None:
            creds = manual

        if creds is not None:
            self.cookies.put(creds)
            logger.info("[qzone_reader] 使用 %s 提供的 QQ空间登录态", creds.source)
        return creds

    async def _credentials_from_protocol(
        self, event: AstrMessageEvent
    ) -> QzoneCredentials | None:
        """从 OneBot 协议端（NapCat）直接取 QQ空间 Cookie。"""
        client = self._get_bot(event)
        if client is None:
            logger.warning("[qzone_reader] 未找到 aiocqhttp 协议端实例")
            return None

        for domain in ("user.qzone.qq.com", "qzone.qq.com"):
            payload = await self._call_get_cookies(client, domain)
            if payload is None:
                continue
            cookie_str = ""
            if isinstance(payload, dict):
                cookie_str = str(payload.get("cookies") or payload.get("cookie") or "")
            creds = credentials_from_cookie_string(cookie_str, source="napcat")
            if creds is not None:
                return creds
        return None

    @staticmethod
    async def _call_get_cookies(client, domain: str):
        """不同 aiocqhttp 版本上 get_cookies 的挂载位置不同，两种都试。"""
        api = getattr(client, "api", None)
        for target in (client, api):
            if target is None or not hasattr(target, "call_action"):
                continue
            try:
                return await target.call_action("get_cookies", domain=domain)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[qzone_reader] %s.call_action(get_cookies, %s) 失败: %s",
                    type(target).__name__,
                    domain,
                    exc,
                )
        logger.warning("[qzone_reader] get_cookies(%s) 调用失败", domain)
        return None

    def _get_bot(self, event: AstrMessageEvent):
        bot = getattr(event, "bot", None)
        if bot is not None and (
            hasattr(bot, "call_action") or hasattr(getattr(bot, "api", None), "call_action")
        ):
            return bot
        # 兜底：通过 Context 拿平台实例的 client
        try:
            platform_id = event.get_platform_id()
            inst = self.context.get_platform_inst(platform_id)
            client = inst.get_client() if inst is not None else None
            if client is not None:
                return client
        except Exception as exc:  # noqa: BLE001
            logger.debug("[qzone_reader] 通过 Context 获取协议端失败: %s", exc)
        return None

    @staticmethod
    def _platform_ok(event: AstrMessageEvent) -> bool:
        """on_llm_request 不经过 event_filters，平台判断只能自己做。"""
        try:
            name = event.get_platform_name()
        except Exception:  # noqa: BLE001
            return True
        return name == "aiocqhttp"

    # -------------------------------------------------------------- 卡片识别

    @classmethod
    def _scan_chain(cls, chain, depth: int = 0) -> tuple[str | None, list[str]]:
        """递归扫描消息链，返回 (分享链接, 降级文案片段)。

        必须递归处理 `Reply.chain`：群聊里「先发卡片、再引用它并 @bot」是常见用法，
        而引用内容不在顶层消息链上，只在 Reply.chain 里。

        `get_reply=True` 是 AstrBot 的默认行为，适配器会用 get_msg 把被引用消息
        抓回来递归解析后放进 Reply.chain（aiocqhttp_platform_adapter.py:303-339），
        所以被引用的卡片是可以拿到的。

        注意：**找到链接后仍要扫完剩余部分**。早期版本命中就返回，导致同一消息里
        链接之后的文字（比如用户 @bot 时说的那句）被整段丢掉。

        depth 限制是为了防 Reply 嵌套自引用导致无限递归。

        Returns:
            (链接或 None, 文本片段列表)。文本片段用于读取失败时的降级文案，
            因此只收用户自己说的话与被引用卡片的内容，不收引用消息的重复文本。
        """
        if depth > 5 or not chain:
            return None, []

        url: str | None = None
        chunks: list[str] = []
        for comp in chain or []:
            if isinstance(comp, Json):
                if url is None:
                    url = extract_share_url(getattr(comp, "data", None))
                card = extract_card_text(getattr(comp, "data", None))
                if card and card not in chunks:
                    chunks.append(card)
            elif isinstance(comp, Plain):
                text = (getattr(comp, "text", "") or "").strip()
                if text:
                    if url is None:
                        # 用户可能直接粘贴链接
                        url = extract_share_url(text)
                    if text not in chunks:
                        chunks.append(text)
            elif isinstance(comp, Reply):
                inner_url, inner_chunks = cls._scan_chain(
                    getattr(comp, "chain", None) or [], depth + 1
                )
                if url is None and inner_url:
                    url = inner_url
                for item in inner_chunks:
                    if item not in chunks:
                        chunks.append(item)
        return url, chunks

    def _find_share_url(self, event: AstrMessageEvent) -> str | None:
        """从消息链（含被引用消息）里找出 QQ空间分享链接。"""
        message_obj = getattr(event, "message_obj", None)
        chain = getattr(message_obj, "message", None) or []

        url, _chunks = self._scan_chain(chain)
        if url:
            return url

        # 纯文本里的链接（含用户直接粘贴的情况）
        url = extract_share_url(getattr(event, "message_str", "") or "")
        if url:
            return url

        # 兜底：从协议端原始事件里捞，覆盖适配器没解析出来的卡片类型
        if message_obj is not None:
            return extract_share_url(getattr(message_obj, "raw_message", None))
        return None

    @classmethod
    def _card_text(cls, event: AstrMessageEvent) -> str:
        """取卡片自带的标题/摘要与用户文本（含被引用消息），作为降级内容。

        直接复用 `_scan_chain`，保证与 `_find_share_url` 的可见范围完全一致 ——
        否则会出现「能认出被引用的卡片、却取不到它的降级文案」这种不对称。
        """
        message_obj = getattr(event, "message_obj", None)
        chain = getattr(message_obj, "message", None) or []
        _url, chunks = cls._scan_chain(chain)
        if chunks:
            return "\n".join(chunks).strip()

        # 消息链里什么都没有时，退回纯文本字段
        return (getattr(event, "message_str", "") or "").strip()

    def _in_scope(self, event: AstrMessageEvent) -> bool:
        whitelist = self.config.get("group_whitelist") or []
        if not whitelist:
            return True
        try:
            session_id = str(event.get_group_id() or event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            return True
        return session_id in {str(item) for item in whitelist}
