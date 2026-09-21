"""QQ空间 Web 接口访问层。

只做最小可用的事情：拿着登录态 Cookie 去查一条说说的完整内容。
接口与参数沿用 QQ空间 Web 端一直在用的那套 cgi，属于社区长期验证过的用法。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import unescape
from http.cookies import SimpleCookie
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from astrbot.api import logger

from . import frontpage

# 说说列表 / 单条详情接口
LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
DETAIL_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

# 分享链接里可能出现的域名，用于从任意卡片 JSON 中捞出真正的说说地址
SHARE_HOST_HINTS = ("qzone.qq.com", "qzonestyle.gtimg.cn")

# 登录态失效时接口返回的错误码
AUTH_ERROR_CODES = {-3000, -10000, -4001}


class QzoneAuthError(RuntimeError):
    """登录态失效，调用方应当作废缓存的 Cookie 并重新获取。"""


@dataclass
class QzoneCredentials:
    """一条可用的 QQ空间登录态。"""

    uin: int
    skey: str = ""
    p_skey: str = ""
    source: str = ""

    @property
    def gtk(self) -> str:
        """由 p_skey 推导出接口需要的 g_tk 令牌。"""
        hash_val = 5381
        for ch in self.p_skey:
            hash_val += (hash_val << 5) + ord(ch)
        return str(hash_val & 0x7FFFFFFF)

    def cookie_header(self) -> str:
        parts = [f"uin=o{self.uin}", f"skey={self.skey}"]
        if self.p_skey:
            parts.append(f"p_skey={self.p_skey}")
        return "; ".join(parts)

    def headers(self, referer: str | None = None) -> dict[str, str]:
        return {
            "User-Agent": BROWSER_UA,
            "Referer": referer or f"https://user.qzone.qq.com/{self.uin}",
            "Origin": "https://user.qzone.qq.com",
            "Cookie": self.cookie_header(),
        }


@dataclass
class QzonePost:
    """一条说说的可读内容。

    转发（repost）要分两层表达：外层是转发者加的评语，
    原文在 original_* 字段里。只有外层会让模型完全看不到原文。
    """

    uin: int = 0
    name: str = ""
    text: str = ""
    rt_text: str = ""
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    created_time: int = 0
    comment_count: int = 0
    like_count: int = 0
    source_name: str = ""
    location: str = ""
    url: str = ""

    # 被转发的原说说（没有转发时全部为空）
    original_name: str = ""
    original_uin: int = 0
    original_time: int = 0
    original_text: str = ""
    original_images: list[str] = field(default_factory=list)

    def is_repost(self) -> bool:
        return bool(self.original_name or self.original_uin or self.original_time)

    def is_empty(self) -> bool:
        return not (
            self.text.strip()
            or self.rt_text.strip()
            or self.original_text.strip()
            or self.images
            or self.videos
            or self.original_images
        )

    def to_prompt(self, *, max_images: int = 0) -> str:
        """渲染成一段给模型看的纯文本。"""
        repost = self.is_repost()
        lines: list[str] = ["【QQ空间说说原文】"]
        if self.name:
            role = "转发者" if repost else "作者"
            lines.append(f"{role}：{self.name}" + (f"（QQ {self.uin}）" if self.uin else ""))
        if self.created_time:
            label = "转发时间" if repost else "发布时间"
            lines.append(f"{label}：{format_time(self.created_time)}")
        if self.source_name:
            lines.append(f"来源：{self.source_name}")
        if self.location:
            lines.append(f"定位：{self.location}")

        body = self.text.strip()
        if not body:
            body = "（这条说说没有文字内容）"
        lines.append("")
        lines.append("转发语：" if repost else "正文：")
        lines.append(body)

        if self.rt_text.strip():
            lines.append("")
            lines.append("引用内容：")
            lines.append(self.rt_text.strip())

        # 转发场景下，被转发的原文才是主体内容
        if repost:
            lines.append("")
            lines.append("—— 以下是这条说说转发的原内容 ——")
            who = self.original_name or "未知作者"
            if self.original_uin:
                who += f"（QQ {self.original_uin}）"
            lines.append(f"原文作者：{who}")
            if self.original_time:
                lines.append(f"原文发布时间：{format_time(self.original_time)}")
            lines.append("")
            lines.append("原文正文：")
            lines.append(
                self.original_text.strip()
                or "（被转发的原说说没有文字内容，或未能读取到）"
            )
            if self.original_images:
                if max_images > 0:
                    shown = min(len(self.original_images), max_images)
                    lines.append(
                        f"原文配图：共 {len(self.original_images)} 张，"
                        f"已附带前 {shown} 张图片。"
                    )
                else:
                    lines.append(
                        f"原文配图：共 {len(self.original_images)} 张（未附带图片内容）。"
                    )

        if self.images:
            if max_images > 0:
                shown = min(len(self.images), max_images)
                lines.append("")
                lines.append(f"配图：共 {len(self.images)} 张，已附带前 {shown} 张图片。")
            else:
                lines.append(f"配图：共 {len(self.images)} 张（未附带图片内容）。")

        if self.videos:
            lines.append(f"含视频：{len(self.videos)} 个（视频内容未解析）。")

        if self.comment_count or self.like_count:
            lines.append(f"互动：{self.like_count} 赞 / {self.comment_count} 评论")

        if self.url:
            lines.append("")
            lines.append(f"原始链接：{self.url}")
        return "\n".join(lines)


def format_time(ts: int) -> str:
    """把秒级时间戳格式化成本地时间字符串。"""
    try:
        from datetime import datetime

        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return str(ts)


def _looks_like_share(url: str) -> bool:
    return "/ugc/share" in url or "mobile.qzone.qq.com/l" in url


def extract_share_url(payload: Any) -> str | None:
    """从卡片 JSON 或纯文本里找出 QQ空间说说地址。

    只说说的分享卡片结构在不同 QQ 版本里并不统一，
    所以这里不假设字段名：递归扫描所有字符串，
    并把形如「看看这个 https://... 很有意思」的文本也一起处理。
    """
    candidates: list[str] = []
    for raw in _walk_strings(payload):
        # QQ 卡片会把逗号转义成 &#44;，先还原，否则 query 参数会被截断
        candidates.extend(_URL_RE.findall(unescape(raw)))

    best: str | None = None
    for url in candidates:
        url = url.strip().rstrip("，。；！？）】》」』、,.;!?)]}>\"'")
        if not any(hint in url for hint in SHARE_HOST_HINTS):
            continue
        # 优先选真正指向某条说说的分享链接
        if _looks_like_share(url):
            return url
        if best is None:
            best = url
    return best


_URL_RE = re.compile(r"https?://[^\s<>\"'，。；！？）】》」』]+", re.IGNORECASE)

# 卡片里可能承载文案的字段名，按可读性排序
_CARD_TEXT_KEYS = ("title", "desc", "description", "summary", "content", "text", "nickname")


def extract_card_text(payload: Any, *, limit: int = 300) -> str:
    """从卡片 JSON 里取出标题/摘要，作为读不到正文时的降级内容。"""
    found: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key in _CARD_TEXT_KEYS
                    and isinstance(value, str)
                    and value.strip()
                    and len(value.strip()) < 500
                ):
                    found.setdefault(key, value.strip())
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    parts: list[str] = []
    for key in _CARD_TEXT_KEYS:
        value = found.get(key)
        if value and value not in parts:
            parts.append(value)
    text = "\n".join(parts).strip()
    return text[:limit]


def _walk_strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_strings(value)


def parse_share_url(url: str) -> dict[str, str]:
    """拆出分享链接的 query 参数（uin / cellid / 时间戳等）。

    QQ空间分享链接的参数分隔符既可能是 `&`，也可能是被转义还原后的 `,`，
    这里两种都按分隔符处理。
    """
    try:
        query = urlsplit(url).query
    except ValueError:
        return {}
    result: dict[str, str] = {}
    for chunk in re.split(r"[&,]", query):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key = key.strip()
        if key and key not in result:
            result[key] = value.strip()
    return result


def credentials_from_cookie_string(cookie_str: str, source: str = "manual") -> QzoneCredentials | None:
    """从 Cookie 字符串里解析出 uin / skey / p_skey。"""
    text = (cookie_str or "").strip()
    if not text:
        return None
    try:
        jar = {k: v.value for k, v in SimpleCookie(text).items()}
    except Exception:  # noqa: BLE001 - Cookie 格式千奇百怪，解析失败就当没有
        return None
    if not jar:
        return None

    uin_text = jar.get("uin") or jar.get("p_uin") or ""
    uin_raw = uin_text[1:] if uin_text[:1].lower() == "o" else uin_text
    uin = int(uin_raw) if uin_raw.isdigit() else 0
    if not uin:
        return None

    p_skey = jar.get("p_skey") or jar.get("skey") or ""
    if not p_skey:
        return None
    return QzoneCredentials(uin=uin, skey=jar.get("skey", ""), p_skey=p_skey, source=source)


async def fetch_post(
    creds: QzoneCredentials,
    share_url: str,
    *,
    timeout: int = 20,
) -> QzonePost | None:
    """按分享链接拉取该条说说的完整内容。

    优先解析 h5 分享页：页面内嵌的 FrontPage 数据里含完整正文、配图，
    并且**转发场景下带有被转发的原文（cell_original）**，
    这是列表接口给不了的。分享页拿不到时才退回列表接口。
    """
    conn_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=conn_timeout) as session:
        # 短链（mobile.qzone.qq.com/l）本身不带 res_uin/cellid，
        # 必须先跟随跳转拿到 h5 分享页地址，否则无法确定是哪条说说。
        final_url, final_params = await _resolve_share(session, creds, share_url)
        if final_url != share_url:
            logger.info("[qzone_reader] 分享短链已解析为: %s", final_url)

        # 首选：直接读分享页
        post = await _fetch_from_share_page(session, creds, final_url or share_url)
        if post is not None and not post.is_empty():
            if post.is_repost():
                logger.info(
                    "[qzone_reader] 这是一条转发：原作者 %s（QQ %s），已连同原文一起读取",
                    post.original_name or "未知",
                    post.original_uin or "未知",
                )
            return post

        logger.info("[qzone_reader] 分享页没取到内容，改走动态列表接口兜底")
        return await _fetch_via_msglist(
            session, creds, share_url, final_url, final_params
        )


async def _fetch_from_share_page(
    session: aiohttp.ClientSession,
    creds: QzoneCredentials,
    url: str,
) -> QzonePost | None:
    """抓 h5 分享页并解析其中的说数据。"""
    for candidate in _share_page_candidates(url):
        html = await _get_html(session, candidate, creds)
        if not html or "FrontPage" not in html:
            continue
        cell = frontpage.extract_share_post(html)
        if not cell:
            continue
        post = _post_from_cell(cell, url=candidate)
        if post is not None:
            return post
    return None


def _share_page_candidates(url: str) -> list[str]:
    """分享页可能有几种等价地址，逐个试。"""
    out: list[str] = []
    if url:
        out.append(url)
        # 有的分享页只在带尾斜杠的 /ugc/share/ 下返回数据
        if "/ugc/share?" in url:
            out.append(url.replace("/ugc/share?", "/ugc/share/?", 1))
        elif "/ugc/share/?" not in url and "/ugc/share/" in url:
            pass
    seen: list[str] = []
    for item in out:
        if item not in seen:
            seen.append(item)
    return seen


def _post_from_cell(cell: dict, *, url: str) -> QzonePost | None:
    """把分享页的一个 cell 转成 QzonePost（含转发的原文）。"""
    if not isinstance(cell, dict):
        return None

    post = QzonePost(url=url)
    post.uin, post.name = frontpage.cell_author(cell)
    post.text = frontpage.cell_text(cell)
    post.created_time = frontpage.cell_time(cell)
    post.images = frontpage.cell_images(cell)
    post.videos = frontpage.cell_video(cell)

    # 转发：外层评语留在 text，原文单独放进 original_* 字段
    original = frontpage.cell_original(cell)
    if original:
        post.original_text = frontpage.cell_text(original)
        post.original_uin, post.original_name = frontpage.cell_author(original)
        post.original_time = frontpage.cell_time(original)
        post.original_images = frontpage.cell_images(original)
        # 外层自己的配图并入原图，保证图片不丢
        for img in post.images:
            if img not in post.original_images:
                post.original_images.append(img)
        post.images = []

    if post.is_empty():
        return None
    return post


async def _fetch_via_msglist(
    session: aiohttp.ClientSession,
    creds: QzoneCredentials,
    share_url: str,
    final_url: str,
    final_params: dict[str, str],
) -> QzonePost | None:
    """兜底路径：用动态列表接口定位并读取。"""
    params = final_params if _has_feed_locator(final_params) else parse_share_url(share_url)

    host_uin = _extract_host_uin(params)
    if not host_uin:
        # 认不出是谁的说说就放弃。绝不退回自己的 uin —— 那会读到
        # 机器人自己空间的第一条说说，并把无关内容注入对话。
        logger.warning(
            "[qzone_reader] 分享链接里没有可用的说说定位参数，放弃读取: %s",
            share_url,
        )
        return None

    cell_id = str(params.get("cellid") or params.get("fid") or "")

    msglist, error = await _fetch_msglist(session, creds, int(host_uin))
    if error in AUTH_ERROR_CODES:
        raise QzoneAuthError(f"QQ空间登录态失效（code={error}）")
    if not msglist:
        return None

    picked = _pick_feed(msglist, cell_id=cell_id, share_url=final_url or share_url)
    if picked is None:
        logger.warning(
            "[qzone_reader] 在 uin=%s 的动态里没找到该条说说（cellid=%s），放弃",
            host_uin,
            cell_id or "无",
        )
        return None

    post = _post_from_feed(picked, share_url=final_url or share_url)

    # 列表接口的正文可能被截断，命中后用详情接口拿全文
    tid = str(picked.get("tid") or "")
    if tid:
        detail = await _fetch_detail(session, creds, int(host_uin), tid)
        if detail:
            post = _post_from_feed(detail, share_url=final_url or share_url, base=post)
    return post


async def _get_html(
    session: aiohttp.ClientSession,
    url: str,
    creds: QzoneCredentials,
) -> str:
    """请求一个页面并解码为文本（分享页可能是 utf-8 或 gbk）。"""
    try:
        async with session.get(
            url, headers=creds.headers(), allow_redirects=True
        ) as resp:
            if resp.status >= 400:
                logger.warning("[qzone_reader] 分享页返回 HTTP %s", resp.status)
                return ""
            raw = await resp.read()
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        logger.warning("[qzone_reader] 抓取分享页失败: %s", exc)
        return ""

    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _has_feed_locator(params: dict[str, str]) -> bool:
    """判断一组参数里是否有能定位具体说说的信息。"""
    if any(params.get(k) for k in ("cellid", "fid", "res_uin", "host_uin")):
        return True
    return _extract_host_uin(params) is not None


def _extract_host_uin(params: dict[str, str]) -> int | None:
    """从参数里取出说说所属账号的 QQ 号。

    只认明确的字段。短链里的 `u=` / `i=` 是内部标识或哈希，不是 QQ 号，
    拿它当 uin 会查到完全无关的账号。
    """
    for key in ("res_uin", "host_uin", "uin"):
        raw = str(params.get(key) or "").strip()
        if raw[:1].lower() == "o":
            raw = raw[1:]
        if raw.isdigit() and 4 < len(raw) <= 12:
            return int(raw)
    return None


async def _resolve_share(
    session: aiohttp.ClientSession,
    creds: QzoneCredentials,
    url: str,
) -> tuple[str, dict[str, str]]:
    """跟随分享短链跳转，返回 (最终地址, 最终地址的参数)。

    失败时原样返回入参，由调用方决定是否放弃。
    """
    try:
        async with session.get(
            url, headers=creds.headers(), allow_redirects=True
        ) as resp:
            final = str(resp.url)
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        logger.warning("[qzone_reader] 解析分享链接失败: %s", exc)
        return url, parse_share_url(url)

    params = parse_share_url(final)
    if _has_feed_locator(params):
        return final, params

    # 有的短链不靠跳转，而是把真实地址放在 HTML 里
    try:
        text = await _get_text(session, url, creds, {})
    except Exception:  # noqa: BLE001
        return final, params
    for found in _URL_RE.findall(text or ""):
        cand = parse_share_url(unescape(found))
        if _has_feed_locator(cand):
            return found, cand
    return final, params


async def _fetch_msglist(
    session: aiohttp.ClientSession,
    creds: QzoneCredentials,
    host_uin: int,
    *,
    num: int = 20,
) -> tuple[list[dict], int | None]:
    """返回 (动态列表, 接口错误码)。"""
    params = {
        "uin": str(host_uin),
        "ftype": "0",
        "sort": "0",
        "pos": "0",
        "num": str(num),
        "replynum": "0",
        "g_tk": creds.gtk,
        "callback": "_preloadCallback",
        "code_version": "1",
        "format": "jsonp",
        "need_comment": "0",
        "need_private_comment": "1",
    }
    text = await _get_text(session, LIST_URL, creds, params)
    payload = _loads_jsonp(text)
    if not isinstance(payload, dict):
        return [], None
    error = payload.get("code")
    msglist = payload.get("msglist")
    if isinstance(msglist, list):
        return [item for item in msglist if isinstance(item, dict)], error if isinstance(error, int) else None
    return [], error if isinstance(error, int) else None


async def _fetch_detail(
    session: aiohttp.ClientSession,
    creds: QzoneCredentials,
    host_uin: int,
    tid: str,
) -> dict | None:
    params = {
        "uin": str(host_uin),
        "tid": tid,
        "format": "jsonp",
        "g_tk": creds.gtk,
    }
    text = await _get_text(session, DETAIL_URL, creds, params)
    payload = _loads_jsonp(text)
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return None


async def _get_text(
    session: aiohttp.ClientSession,
    url: str,
    creds: QzoneCredentials,
    params: dict[str, str],
) -> str:
    try:
        async with session.get(url, params=params, headers=creds.headers()) as resp:
            if resp.status >= 400:
                logger.warning("[qzone_reader] 接口返回 HTTP %s: %s", resp.status, url)
                return ""
            raw = await resp.read()
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        logger.warning("[qzone_reader] 请求 QQ空间接口失败: %s", exc)
        return ""
    # QQ空间接口有时返回 GBK
    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _loads_jsonp(text: str) -> Any:
    """把 jsonp 回调包成真正的 JSON 再解析。"""
    body = (text or "").strip()
    if not body:
        return None
    if body.startswith("{"):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    start = body.find("(")
    end = body.rfind(")")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(body[start + 1 : end])
    except json.JSONDecodeError:
        return None


def _pick_feed(
    msglist: list[dict],
    *,
    cell_id: str,
    share_url: str,
) -> dict | None:
    """在动态列表里定位分享的那一条。"""
    if cell_id:
        for feed in msglist:
            if str(feed.get("cellid") or "") == cell_id:
                return feed

    # 兜底：用分享链接里的时间戳去比对
    params = parse_share_url(share_url)
    for key in ("begintime", "t", "time"):
        raw = params.get(key) or ""
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) >= 10:
            want = int(digits[:10])
            for feed in msglist:
                created = _as_int(feed.get("created_time"))
                if created and abs(created - want) <= 120:
                    return feed

    # 定位不到就返回 None。
    # 这里绝不能用 msglist[0] 兜底：拿不准具体是哪一条时随便挑一条，
    # 会把完全无关的说说当成用户转发的内容注入对话。
    return None


def _post_from_feed(
    feed: dict,
    *,
    share_url: str,
    base: QzonePost | None = None,
) -> QzonePost:
    """把接口返回的一条 feed 转成 QzonePost。"""
    post = base or QzonePost()
    post.url = share_url
    post.uin = _as_int(feed.get("uin")) or post.uin
    post.name = str(feed.get("name") or post.name or "")
    content = str(feed.get("content") or "").strip()
    if content:
        post.text = content
    post.created_time = _as_int(feed.get("created_time")) or post.created_time
    post.comment_count = _as_int(feed.get("cmtnum")) or post.comment_count
    post.like_count = _as_int(feed.get("usenum")) or post.like_count
    post.source_name = str(feed.get("source_name") or post.source_name or "")

    rt_con = feed.get("rt_con")
    if isinstance(rt_con, dict):
        rt_text = str(rt_con.get("content") or "").strip()
        if rt_text:
            post.rt_text = rt_text

    images = list(post.images)
    for item in feed.get("pic") or []:
        if not isinstance(item, dict):
            continue
        url = _best_image_url(item)
        if url and url not in images:
            images.append(url)
    post.images = images

    videos = list(post.videos)
    for key in ("video", "rt_video"):
        item = feed.get(key)
        if isinstance(item, dict):
            url = _best_image_url(item)
            if url and url not in videos:
                videos.append(url)
    post.videos = videos
    return post


def _best_image_url(item: dict) -> str | None:
    """在一张图片的多个尺寸里挑最大的那个。"""
    for key in ("url3", "url2", "url1", "raw", "picrawurl", "originurl", "bigurl"):
        value = item.get(key)
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            return value.strip()
    return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class CookieCache:
    """缓存一份登录态，避免每条消息都去问协议端要 Cookie。"""

    def __init__(self, ttl: int):
        self._ttl = max(int(ttl), 0)
        self._creds: QzoneCredentials | None = None
        self._at: float = 0.0

    def get(self) -> QzoneCredentials | None:
        if self._creds is None:
            return None
        if self._ttl > 0 and monotonic() - self._at >= self._ttl:
            return None
        return self._creds

    def put(self, creds: QzoneCredentials) -> None:
        self._creds = creds
        self._at = monotonic()

    def clear(self) -> None:
        self._creds = None
        self._at = 0.0
