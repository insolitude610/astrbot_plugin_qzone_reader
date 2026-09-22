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
from urllib.parse import urljoin, urlsplit

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

# 允许作为「说说分享地址」被抓取的域名。
# 必须按解析后的 hostname 精确匹配（或子域），不能对整条 URL 做子串判断 ——
# 否则 https://evil.example/?x=qzone.qq.com 这种链接也会被当成 QQ空间地址，
# 而请求会带上账号 Cookie，等于把登录态发给任意主机。
TRUSTED_SHARE_HOSTS = ("qzone.qq.com", "qzonestyle.gtimg.cn")

# 允许接收登录态 Cookie 的域名。除 QQ空间自身外，还包括真实说说里出现过的图床
# （见 tests/fixtures/repost_cell.json 的 m.qpic.cn / r.photo.store.qq.com）；
# 不带上它们，配图会因为缺少登录态而下载失败。
CREDENTIAL_HOSTS = ("qzone.qq.com", "qpic.cn", "qlogo.cn", "gtimg.cn", "photo.store.qq.com")

# 跟随跳转的跳数上限，与 aiohttp 的默认值保持一致
MAX_REDIRECT_HOPS = 10
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# 登录态失效时接口返回的错误码
AUTH_ERROR_CODES = {-3000, -10000, -4001}

# 登录态缓存默认时长（秒）
DEFAULT_COOKIE_TTL = 600

# 交代配图的结构，让模型能把「正文里的 pN」和「看到的第 N 个图片块」对上。
# 不写这段的话，模型只会看到一串图片块，无法知道它们对应原文的第几张图。
IMAGE_LAYOUT_HINT = (
    "（说明：长截图会被按高度切成多个片段依次附带，"
    "所以附带的图片块数量可能多于上面的配图张数。"
    "片段按顺序排列，相邻片段内容有少量重叠；同一张原图切出的片段紧挨在一起。"
    "正文里用 p1、p2 这类编号引用的「第几张图」，"
    "对应的是上面说的配图张数顺序，不是图片块序号 —— 请按这个对应关系描述。）"
)


class QzoneAuthError(RuntimeError):
    """登录态失效，调用方应当作废缓存的 Cookie 并重新获取。"""


def _host_matches(url: Any, domains: tuple[str, ...]) -> bool:
    """URL 的 hostname 是否落在给定域名（或其子域）内。

    刻意用解析后的 hostname，而不是对整条 URL 做子串判断 —— 后者会把
    `https://evil.example/?x=qzone.qq.com` 这种地址也放进来，而带着 Cookie
    的请求一旦发出去，账号登录态就泄露了。任何解析失败都按不匹配处理。
    """
    try:
        parts = urlsplit(str(url))
    except (ValueError, TypeError):
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def is_trusted_qzone_url(url: Any) -> bool:
    """是否是可信的 QQ空间分享地址（决定能不能去抓、能不能当分享页解析）。"""
    return _host_matches(url, TRUSTED_SHARE_HOSTS)


def may_send_credentials(url: Any) -> bool:
    """该地址是否可以携带登录态 Cookie（决定凭据能发到哪里）。"""
    return _host_matches(url, CREDENTIAL_HOSTS)


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

    def headers(self, referer: str | None = None, *, url: Any = None) -> dict[str, str]:
        """构造请求头。

        只有明确传入了 url、且该 url 落在 CREDENTIAL_HOSTS 内时才会带 Cookie。
        「不传 url 就不带凭据」是刻意设计的（fail-closed）：忘记传参只会让这次
        请求没有登录态，而不会把账号 Cookie 发到不该发的地方。
        """
        headers = {
            "User-Agent": BROWSER_UA,
            "Referer": referer or f"https://user.qzone.qq.com/{self.uin}",
            "Origin": "https://user.qzone.qq.com",
        }
        if url is not None and may_send_credentials(url):
            headers["Cookie"] = self.cookie_header()
        return headers


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

    def to_prompt(self, *, max_images: int = 0, covered_images: int | None = None) -> str:
        """渲染成一段给模型看的纯文本。

        Args:
            max_images: 是否附图（>0 表示附图），同时作为未传 covered_images 时的兜底。
            covered_images: **实际被覆盖到的候选图张数**（按候选取图顺序）。
                长图会被切成多个图片块，块数远多于原图张数，所以不能用图片块数
                来声称「已附带前几张原图」—— 那会让模型以为它拿到了后面的图。
                传 None 时退回旧行为（用 max_images 估算），仅用于兼容旧调用。
        """
        repost = self.is_repost()
        if covered_images is None:
            covered_images = max_images
        covered_images = max(covered_images, 0)
        covered_original = min(len(self.original_images), covered_images)
        covered_own = min(
            len(self.images), max(covered_images - len(self.original_images), 0)
        )
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
                    lines.append(
                        f"原文配图：共 {len(self.original_images)} 张，"
                        f"已附带前 {covered_original} 张。"
                    )
                    lines.append(IMAGE_LAYOUT_HINT)
                else:
                    lines.append(
                        f"原文配图：共 {len(self.original_images)} 张（未附带图片内容）。"
                    )

        if self.images:
            if max_images > 0:
                lines.append("")
                lines.append(f"配图：共 {len(self.images)} 张，已附带前 {covered_own} 张。")
                if not (self.is_repost() and self.original_images):
                    lines.append(IMAGE_LAYOUT_HINT)
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
        if not is_trusted_qzone_url(url):
            # 域名必须落在 QQ空间白名单内。这里不能用「整串包含 qzone.qq.com」
            # 之类的子串判断 —— 攻击者可以在自己域名后拼一个参数来通过它，
            # 之后插件会带着账号 Cookie 去请求那台主机。
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
    if not is_trusted_qzone_url(share_url):
        # 入口就挡住非 QQ空间地址：即使上游的链接识别将来出问题，
        # 也不会有一条带着账号 Cookie 的请求发往白名单之外的主机。
        logger.warning("[qzone_reader] 非 QQ空间域名，拒绝读取: %s", share_url)
        return None
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
    """分享页可能有几种等价地址，逐个试。

    有的分享页只在带尾斜杠的 `/ugc/share/` 下返回数据，也有的只在不带尾斜杠的
    `/ugc/share` 下返回数据，所以两个方向都要补一个候选。三个分支互斥，
    不会产出 `??` 这类畸形地址。
    """
    out: list[str] = []
    if url:
        out.append(url)
        if "/ugc/share?" in url:
            out.append(url.replace("/ugc/share?", "/ugc/share/?", 1))
        elif "/ugc/share/?" in url:
            out.append(url.replace("/ugc/share/?", "/ugc/share?", 1))
        elif "/ugc/share/" in url:
            out.append(url.replace("/ugc/share/", "/ugc/share?", 1))
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


async def fetch_trusted(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, str] | None = None,
    creds: QzoneCredentials | None = None,
    total_timeout: int = 20,
    max_hops: int = MAX_REDIRECT_HOPS,
) -> tuple[str, int, bytes]:
    """GET 一个地址，并手动逐跳跟随跳转。

    为什么要自己跟随跳转、而不用 `allow_redirects=True`：
    aiohttp 在跳转时会把原始请求头原样带到新地址（只去掉 Authorization），
    所以一旦把 Cookie 设在请求头里，跨主机跳转就等于把登录态发给了跳转目标。
    这里每一跳都重新决定请求头，凭据只会发往 CREDENTIAL_HOSTS 内的主机。

    另外两点与 aiohttp 对齐：
    - 首跳之后不再携带原始 query，避免把 uin / g_tk 带到跳转目标；
    - 整条跳转链共用一个总超时，而不是每跳重新计时。

    Returns:
        (最后一次真正请求到的地址, HTTP 状态码, 响应体)。
        超时、跳数超限或请求异常时状态码为 0、响应体为空。
    """
    current = url
    current_params = params
    fetched = url
    started = monotonic()
    for _ in range(max_hops + 1):
        remaining = total_timeout - (monotonic() - started)
        if remaining <= 0:
            logger.warning("[qzone_reader] 跟随跳转超时: %s", url)
            return fetched, 0, b""
        headers = creds.headers(url=current) if creds is not None else {}
        fetched = current
        try:
            async with session.get(
                current,
                params=current_params,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=remaining),
            ) as resp:
                if resp.status in REDIRECT_STATUSES:
                    location = (resp.headers or {}).get("Location")
                    try:
                        await resp.read()
                    except Exception:  # noqa: BLE001 - 只为释放连接
                        pass
                    if not location:
                        return fetched, resp.status, b""
                    current = urljoin(current, str(location))
                    current_params = None
                    continue
                if resp.status >= 400:
                    return fetched, resp.status, b""
                return fetched, resp.status, await resp.read()
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            logger.warning("[qzone_reader] 请求失败: %s（%s）", exc, current)
            return fetched, 0, b""

    logger.warning("[qzone_reader] 跳转次数超过上限（%d）: %s", max_hops, url)
    return fetched, 0, b""


async def _get_html(
    session: aiohttp.ClientSession,
    url: str,
    creds: QzoneCredentials,
) -> str:
    """请求一个页面并解码为文本（分享页可能是 utf-8 或 gbk）。"""
    final, status, raw = await fetch_trusted(session, url, creds=creds)
    if status >= 400:
        logger.warning("[qzone_reader] 分享页返回 HTTP %s", status)
        return ""
    if not is_trusted_qzone_url(final):
        # 跳到了白名单之外的地址：既不能信它的内容（可能伪造 FrontPage），
        # 也必须明确放弃这一页。
        logger.warning("[qzone_reader] 分享页跳转到了非 QQ空间地址，放弃: %s", final)
        return ""
    if not raw:
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

    只认明确的字段：分享页的 `res_uin` 与列表接口的 `host_uin`。
    短链里的 `u=` / `i=` 是内部标识或哈希；裸 `uin` 也可能是分享者而非作者 ——
    认不准时必须放弃（宁可读不到，也不能去别人的动态列表里按时间戳猜一条）。
    """
    for key in ("res_uin", "host_uin"):
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

    失败或跳到非 QQ空间地址时原样返回入参，由调用方决定是否放弃。
    """
    final, _status, _body = await fetch_trusted(session, url, creds=creds)
    if not is_trusted_qzone_url(final):
        logger.warning("[qzone_reader] 分享链接跳转到了非 QQ空间地址，忽略跳转: %s", final)
        final = url

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
    final, status, raw = await fetch_trusted(session, url, params=params, creds=creds)
    if status >= 400:
        logger.warning("[qzone_reader] 接口返回 HTTP %s: %s", status, url)
        return ""
    if not is_trusted_qzone_url(final):
        logger.warning("[qzone_reader] 接口跳转到了非 QQ空间地址，放弃: %s", final)
        return ""
    if not raw:
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

    def __init__(self, ttl: int = DEFAULT_COOKIE_TTL):
        # 这个类在插件构造时就会被实例化，所以解析失败绝不能抛异常 ——
        # AstrBot 的插件加载只对 TypeError 重试，ValueError 会导致整个插件加载失败。
        self._ttl = _parse_cookie_ttl(ttl)
        self._creds: QzoneCredentials | None = None
        self._at: float = 0.0

    def get(self) -> QzoneCredentials | None:
        if self._creds is None:
            return None
        if self._ttl <= 0:
            # 0 表示不缓存（README 与配置 schema 的承诺）。
            return None
        if monotonic() - self._at >= self._ttl:
            return None
        return self._creds

    def put(self, creds: QzoneCredentials) -> None:
        self._creds = creds
        self._at = monotonic()

    def clear(self) -> None:
        self._creds = None
        self._at = 0.0


def _parse_cookie_ttl(ttl: Any) -> int:
    """解析 cookie_ttl，非法值回退默认值（绝不抛异常）。"""
    try:
        return max(int(ttl), 0)
    except (TypeError, ValueError):
        return DEFAULT_COOKIE_TTL
