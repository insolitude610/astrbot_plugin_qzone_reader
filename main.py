"""把转发过来的 QQ空间说说读进对话上下文。

流程：识别转发卡片 -> 用 NapCat 登录态拉取说说全文 -> 在 on_llm_request 里
把内容作为额外用户内容块注入。由于 AstrBot 会把「prompt + 额外内容块 + 图片」
组装成同一条 user 消息并写入会话历史，注入的内容天然进入上下文，
之后可以继续追问讨论。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Json, Plain

from .core.qzone_api import (
    CookieCache,
    QzoneAuthError,
    QzoneCredentials,
    credentials_from_cookie_string,
    extract_card_text,
    extract_share_url,
    fetch_post,
)

# 一次注入里附带的图片上限，防止把模型上下文撑爆
HARD_IMAGE_CAP = 9

# summarize_mode = brief：要点式总结后自然接话（默认，最省）
BRIEF_SUMMARY_INSTRUCTION = (
    "用户转发了一条 QQ空间说说，上面的【QQ空间说说原文】就是该说说的内容。\n"
    "请先简要总结这条说说讲了什么（作者的表达、情绪或重点），"
    "然后用自然的口吻回应；用户接下来可能会继续追问或和你讨论这条说说。"
)

# summarize_mode = full：以总结为主体，完整讲清楚
FULL_SUMMARY_INSTRUCTION = (
    "用户转发了一条 QQ空间说说，上面的【QQ空间说说原文】就是该说说的内容。\n"
    "请完整地总结这条说说，用你平时说话的口吻，但内容要讲全：\n"
    "1. 谁说给谁听的、发布与转发的时间；\n"
    "2. 事情的来龙去脉，按条理把要点讲清楚；\n"
    "3. 涉及哪些人或方，各自的主张与立场；\n"
    "4. 作者的情绪与意图；\n"
    "5. 若有配图，说明图片内容与正文的关系；\n"
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
        self.cookies = CookieCache(self.config.get("cookie_ttl", 600))
        # 每一次用户消息都是一批新的注入，键是 event 对象本身
        self._pending: dict[int, tuple[str, list[str]]] = {}

    # ------------------------------------------------------------------ 读取

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def capture_qzone_share(self, event: AstrMessageEvent):
        """识别 QQ空间分享卡片，先把正文和图片准备好，等注入阶段使用。"""
        if not self._in_scope(event):
            return

        share_url = self._find_share_url(event)
        if not share_url:
            return

        logger.info("[qzone_reader] 检测到 QQ空间分享，开始读取: %s", share_url)

        text, images = await self._build_payload(event, share_url)
        if text:
            self._pending[id(event)] = (text, images)

    @filter.on_llm_request()
    async def inject_qzone_content(self, event: AstrMessageEvent, req: ProviderRequest):
        """把说说内容作为额外用户内容块注入，从而进入本轮对话并落进历史。

        注意：on_llm_request 不经过 event_filters，平台/会话过滤必须在这里手动做。
        另外这里绝不能调用 stop_event()，否则本轮消息不会被写入历史。
        """
        payload = self._pending.pop(id(event), None)
        if payload is None:
            return
        if not self._platform_ok(event):
            return
        text, images = payload

        try:
            self._append_extra_part(req, text)
            for url in images:
                req.image_urls.append(url)
        except Exception as exc:  # noqa: BLE001 - 注入失败不应打断对话
            logger.warning("[qzone_reader] 注入说说内容失败: %s", exc)

    # ------------------------------------------------------------------ 组装

    async def _build_payload(
        self, event: AstrMessageEvent, share_url: str
    ) -> tuple[str, list[str]]:
        """拉取说说内容并渲染成待注入的文本与图片列表。"""
        max_images = max(int(self.config.get("max_images", 4) or 0), 0)
        mode = self._summarize_mode()
        notify = bool(self.config.get("notify_on_failure", True))

        post = None
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
        images: list[str] = []
        if max_images > 0:
            cap = min(max_images, HARD_IMAGE_CAP)
            for url in [*post.original_images, *post.images]:
                if url not in images:
                    images.append(url)
                if len(images) >= cap:
                    break

        body = post.to_prompt(max_images=len(images))
        return self._with_instruction(body, mode), images

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
    def _append_extra_part(req: ProviderRequest, text: str) -> None:
        """优先用 ContentPart 对象，取不到就退回等价 dict。"""
        try:
            from astrbot.core.agent.message import TextPart

            part = TextPart(text=text)
        except Exception:  # noqa: BLE001
            part = {"type": "text", "text": text}
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

    def _find_share_url(self, event: AstrMessageEvent) -> str | None:
        """从消息链里找出 QQ空间分享链接。"""
        message_obj = getattr(event, "message_obj", None)
        chain = getattr(message_obj, "message", None) or []

        plain_chunks: list[str] = []
        for comp in chain:
            if isinstance(comp, Json):
                url = extract_share_url(getattr(comp, "data", None))
                if url:
                    return url
            elif isinstance(comp, Plain):
                text = getattr(comp, "text", "") or ""
                if text:
                    plain_chunks.append(text)

        # 纯文本里的链接（含用户直接粘贴的情况）
        url = extract_share_url("\n".join(plain_chunks))
        if url:
            return url

        url = extract_share_url(getattr(event, "message_str", "") or "")
        if url:
            return url

        # 兜底：从协议端原始事件里捞，覆盖适配器没解析出来的卡片类型
        if message_obj is not None:
            return extract_share_url(getattr(message_obj, "raw_message", None))
        return None

    @staticmethod
    def _card_text(event: AstrMessageEvent) -> str:
        """取卡片自带的标题/摘要与文本，作为读取失败时的降级内容。"""
        message_obj = getattr(event, "message_obj", None)
        chain = getattr(message_obj, "message", None) or []
        chunks: list[str] = []
        for comp in chain:
            if isinstance(comp, Json):
                card = extract_card_text(getattr(comp, "data", None))
                if card:
                    chunks.append(card)
            elif isinstance(comp, Plain):
                text = (getattr(comp, "text", "") or "").strip()
                if text:
                    chunks.append(text)
        return "\n".join(chunks).strip()

    def _in_scope(self, event: AstrMessageEvent) -> bool:
        whitelist = self.config.get("group_whitelist") or []
        if not whitelist:
            return True
        try:
            session_id = str(event.get_group_id() or event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            return True
        return session_id in {str(item) for item in whitelist}
