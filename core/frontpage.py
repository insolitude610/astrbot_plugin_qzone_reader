"""解析 h5.qzone.qq.com 分享页内嵌的 FrontPage 数据。

分享页里有一段 `FrontPage = {...}` 赋值，里面 `data.data` 就是这条说说的
完整结构（cell_comm / cell_userinfo / cell_summary / cell_pic / cell_original ...）。

它是 JS 对象字面量而不是 JSON：键没有引号、单双引号混用，可能还有注释。
所以不能直接 json.loads，需要先按括号配平把对象切出来，再提取 data 字段。

相比 old 的列表接口方案，这条路的好处：
- 短链解析后的 res_uin / cellid 就是分享页自己的标识，直接可用
- 转发（repost）的原内容在 cell_original 里，可递归取到，不会只剩外层评论
- 图片在 cell_pic.picdata[].photourl 里有多档尺寸，可以挑最大的
"""

from __future__ import annotations

import json
import re
from typing import Any

# 分享页里承载数据的变量名
_FRONTPAGE_MARKER = "FrontPage"

# 图片尺寸档位，按「越大越优先」排序
_PHOTO_SIZE_PRIORITY = ("0", "1", "2", "3", "4")
# 明确表示原图/大图的 URL 特征
_ORIGIN_HINTS = ("/o&", "/b&", "origin", "raw", "large")


def extract_share_post(html: str) -> dict[str, Any] | None:
    """从分享页 HTML 中取出说说数据。

    Args:
        html: 分享页 HTML 文本。

    Returns:
        说说的 cell 字典（含 cell_comm / cell_userinfo / ...），取不到返回 None。
    """
    payload = _extract_frontpage_payload(html)
    if payload is None:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    return data or None


def _extract_frontpage_payload(html: str) -> dict[str, Any] | None:
    """定位 FrontPage 赋值并解出它的 data 字段。"""
    if not html or _FRONTPAGE_MARKER not in html:
        return None

    for start in _frontpage_object_starts(html):
        outer = _balanced_object(html, start)
        if not outer:
            continue
        raw = _extract_top_level_value(outer, "data")
        if not raw:
            continue
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def _frontpage_object_starts(source: str):
    """找出所有 `FrontPage = {` 里 `{` 的位置。"""
    index = 0
    while True:
        index = source.find(_FRONTPAGE_MARKER, index)
        if index == -1:
            return
        before = source[index - 1] if index else ""
        end = index + len(_FRONTPAGE_MARKER)
        # 避免匹配到 xxxFrontPage 这种更长标识符的一部分
        if before and (before.isalnum() or before in "_$"):
            index = end
            continue
        cursor = end
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor < len(source) and source[cursor] == "=":
            cursor += 1
            while cursor < len(source) and source[cursor].isspace():
                cursor += 1
            if cursor < len(source) and source[cursor] == "{":
                yield cursor
        index = end


def _balanced_object(source: str, start: int) -> str | None:
    """从 start 的 `{` 开始，按配平取出一整个对象字面量。

    会正确跳过字符串（含转义）与 //、/* */ 注释。
    """
    if start < 0 or start >= len(source) or source[start] != "{":
        return None
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = start
    while index < len(source):
        char = source[index]
        nxt = source[index + 1 : index + 2]
        if line_comment:
            if char in "\r\n":
                line_comment = False
        elif block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 1
        elif quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {'"', "'", "`"}:
            quote = char
        elif char == "/" and nxt == "/":
            line_comment = True
            index += 1
        elif char == "/" and nxt == "*":
            block_comment = True
            index += 1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
        index += 1
    return None


def _extract_top_level_value(outer: str, key: str) -> str | None:
    """在对象字面量的第一层里取出 `key: {...}` 的对象文本。"""
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = 0
    while index < len(outer):
        char = outer[index]
        nxt = outer[index + 1 : index + 2]

        if line_comment:
            if char in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 1
            index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue

        matched = False
        if depth == 1:
            for candidate in (f'"{key}"', f"'{key}'", key):
                if not outer.startswith(candidate, index):
                    continue
                before = outer[index - 1] if index else ""
                after = outer[index + len(candidate) : index + len(candidate) + 1]
                if candidate == key and (
                    (before and (before.isalnum() or before in "_$"))
                    or (after and (after.isalnum() or after in "_$"))
                ):
                    continue
                matched = True
                break
        if matched:
            cursor = index + len(key)
            if outer[index : index + 1] in {'"', "'"}:
                cursor = index + len(key) + 2
            while cursor < len(outer) and outer[cursor].isspace():
                cursor += 1
            if cursor < len(outer) and outer[cursor] == ":":
                cursor += 1
                while cursor < len(outer) and outer[cursor].isspace():
                    cursor += 1
                if cursor < len(outer) and outer[cursor] == "{":
                    return _balanced_object(outer, cursor)
            index = cursor
            continue

        if char in {'"', "'", "`"}:
            quote = char
        elif char == "/" and nxt == "/":
            line_comment = True
            index += 1
        elif char == "/" and nxt == "*":
            block_comment = True
            index += 1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return None


# ------------------------------------------------------------------ 字段读取


def cell_text(cell: dict[str, Any]) -> str:
    """取一个 cell 的正文。"""
    summary = cell.get("cell_summary")
    if isinstance(summary, dict):
        text = summary.get("summary")
        if isinstance(text, str):
            return text.strip()
    return ""


def cell_time(cell: dict[str, Any]) -> int:
    """取一个 cell 的发布时间。"""
    comm = cell.get("cell_comm")
    if isinstance(comm, dict):
        for key in ("time", "lastmodify_time"):
            value = comm.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return 0


def cell_author(cell: dict[str, Any]) -> tuple[int, str]:
    """取一个 cell 的作者 (uin, 昵称)。"""
    info = cell.get("cell_userinfo")
    if not isinstance(info, dict):
        return 0, ""
    user = info.get("user")
    if not isinstance(user, dict):
        return 0, ""
    uin = user.get("uin")
    name = user.get("nickname")
    return (
        uin if isinstance(uin, int) else 0,
        name.strip() if isinstance(name, str) else "",
    )


def cell_images(cell: dict[str, Any], *, limit: int = 0) -> list[str]:
    """取一个 cell 的配图，尽量拿大图。

    Args:
        cell: 说说 cell。
        limit: 最多返回几张，<=0 表示不限。
    """
    pic = cell.get("cell_pic")
    if not isinstance(pic, dict):
        return []
    picdata = pic.get("picdata")
    if not isinstance(picdata, list):
        return []

    urls: list[str] = []
    for item in picdata:
        if not isinstance(item, dict):
            continue
        url = _best_photo_url(item)
        if url and url not in urls:
            urls.append(url)
    if limit > 0:
        return urls[:limit]
    return urls


def _best_photo_url(item: dict[str, Any]) -> str | None:
    """在一张图的多个尺寸里挑最大的那张。"""
    photos = item.get("photourl")
    if isinstance(photos, dict):
        best: str | None = None
        best_score = -1
        for key, value in photos.items():
            if not isinstance(value, dict):
                continue
            url = value.get("url")
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            width = value.get("width") if isinstance(value.get("width"), int) else 0
            height = value.get("height") if isinstance(value.get("height"), int) else 0
            score = width * height
            # 明确的原图特征再加分
            if any(hint in url for hint in _ORIGIN_HINTS):
                score += 10_000_000
            if str(key) == "0":
                score += 1_000_000
            if score > best_score:
                best_score = score
                best = url
        if best:
            return best

    # 退路：direct 字段
    for key in ("raw", "sloc"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def cell_video(cell: dict[str, Any]) -> list[str]:
    """取 cell 里的视频地址。"""
    pic = cell.get("cell_pic")
    if not isinstance(pic, dict):
        return []
    picdata = pic.get("picdata")
    if not isinstance(picdata, list):
        return []
    urls: list[str] = []
    for item in picdata:
        if not isinstance(item, dict):
            continue
        flag = item.get("videoflag")
        if not (flag == 1 or flag == "1"):
            continue
        video = item.get("videodata")
        if not isinstance(video, dict):
            continue
        for key in ("videourl", "url", "download_url"):
            value = video.get(key)
            if isinstance(value, str) and value.startswith("http"):
                if value not in urls:
                    urls.append(value)
                break
    return urls


def cell_original(cell: dict[str, Any]) -> dict[str, Any] | None:
    """取被转发的原说说 cell（没有则 None）。"""
    original = cell.get("cell_original")
    if isinstance(original, dict) and original:
        return original
    return None


def is_repost(cell: dict[str, Any]) -> bool:
    """判断这条说说是不是转发。"""
    return cell_original(cell) is not None


__all__ = [
    "extract_share_post",
    "cell_text",
    "cell_time",
    "cell_author",
    "cell_images",
    "cell_video",
    "cell_original",
    "is_repost",
]
