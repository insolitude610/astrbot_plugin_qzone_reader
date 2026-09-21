"""长截图切片。

QQ空间的瓜条配图常常是「聊天记录长截图」，典型尺寸 640x3799、476x4096。
这类图直接丢给视觉模型会被等比缩放到像素上限（通常约 1024px），
高度压缩 3~4 倍后聊天文字彻底糊掉，模型实际上读不到内容。

做法：保持宽度不变（文字清晰度只取决于宽度），只按高度切成若干段，
段间保留重叠以免把一条消息切成两半，再以 data URL 形式交给模型。
"""

from __future__ import annotations

import asyncio
import base64
import io
import math

from astrbot.api import logger

# 超过这个高度就认为是"长截图"，值得切片
DEFAULT_TALL_THRESHOLD = 1600
# 每片的目标最大高度。多数视觉模型在这个尺寸内不会明显降采样
DEFAULT_SLICE_HEIGHT = 1280
# 相邻切片的重叠像素，避免把一条消息拦腰截断
DEFAULT_OVERLAP = 80
# 单张图最多切几片，防止极端长图把预算吃光
MAX_SLICES_PER_IMAGE = 12

# 视觉模型对超长图会降采样，也顺带防一下体积
JPEG_QUALITY = 88


def _import_pillow():
    """延迟导入 Pillow，缺库时由调用方降级而不是让插件加载失败。"""
    try:
        from PIL import Image  # noqa: PLC0415

        return Image
    except Exception:  # noqa: BLE001
        return None


def is_tall(width: int, height: int, *, threshold: int = DEFAULT_TALL_THRESHOLD) -> bool:
    """判断是否属于需要切片的长截图。"""
    return height >= threshold and height > width * 1.5


def slice_image(
    data: bytes,
    *,
    slice_height: int = DEFAULT_SLICE_HEIGHT,
    overlap: int = DEFAULT_OVERLAP,
    max_slices: int = MAX_SLICES_PER_IMAGE,
) -> list[str]:
    """把一张长图切成若干 data URL。不适合切片或失败时返回空列表。

    Args:
        data: 原始图片字节。
        slice_height: 每片的目标最大高度。
        overlap: 相邻片的重叠像素。
        max_slices: 单图最多切几片。

    Returns:
        data URL 列表；无需切片或处理失败时为空列表。
    """
    Image = _import_pillow()
    if Image is None:
        logger.warning("[qzone_reader] 未安装 Pillow，无法切片长图（pip install pillow）")
        return []

    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            width, height = img.size
            if not is_tall(width, height):
                return []
            if not slice_height or slice_height <= 0:
                return []

            step = max(slice_height - max(overlap, 0), 1)
            count = min(math.ceil(height / step), max_slices)

            # 统一转 RGB，避免 PNG 透明通道与调色板模式导致 JPEG 保存失败
            frame = img.convert("RGB")

            slices: list[str] = []
            for index in range(count):
                top = index * step
                bottom = min(top + slice_height, height)
                if top >= height:
                    break
                piece = frame.crop((0, top, width, bottom))

                buffer = io.BytesIO()
                piece.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                slices.append(f"data:image/jpeg;base64,{encoded}")
                if bottom >= height:
                    break
            return slices
    except Exception as exc:  # noqa: BLE001 - 任何解码失败都退回原图
        logger.warning("[qzone_reader] 切片失败，将按原图发送: %s", exc)
        return []


async def fetch_bytes(
    session,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> bytes | None:
    """下载图片字节，失败返回 None。"""
    try:
        async with session.get(
            url,
            headers=headers or {},
            allow_redirects=True,
            timeout=timeout,
        ) as resp:
            if resp.status >= 400:
                logger.debug("[qzone_reader] 图片下载 HTTP %s", resp.status)
                return None
            return await resp.read()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[qzone_reader] 图片下载失败: %s", exc)
        return None


async def prepare_images(
    session,
    urls: list[str],
    *,
    budget: int,
    headers: dict[str, str] | None = None,
    slice_tall: bool = True,
    slice_height: int = DEFAULT_SLICE_HEIGHT,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """按预算准备图片：先放原图，剩余额度用于长截图切片。

    `budget` 是最终交给模型的图片总数上限，切片计入其中 ——
    否则一本瓜条的 14 张长图能切成 50 多片，token 会失控。

    Args:
        session: 复用的 aiohttp 会话。
        urls: 候选图片地址，按优先级排列。
        budget: 图片总数上限，<=0 返回空列表。
        headers: 下载图片用的请求头（QQ空间图需要 Referer）。
        slice_tall: 是否对长截图切片。
        slice_height: 每片目标高度。
        overlap: 相邻片重叠像素。

    Returns:
        交给模型的图片引用列表（原 URL 或 data URL）。
    """
    if budget <= 0 or not urls:
        return []

    result: list[str] = []
    for url in urls:
        if len(result) >= budget:
            break

        # 额度足够时，先按原图放，避免无谓的下载与切片
        if not slice_tall:
            result.append(url)
            continue

        data = await fetch_bytes(session, url, headers=headers)
        if data is None:
            # 下载不到就直接把原 URL 交给上层，让它自己处理
            result.append(url)
            continue

        Image = _import_pillow()
        size: tuple[int, int] | None = None
        if Image is not None:
            try:
                with Image.open(io.BytesIO(data)) as img:
                    size = img.size
            except Exception:  # noqa: BLE001
                size = None

        if size is None or not is_tall(size[0], size[1]):
            result.append(url)
            continue

        # CPU 密集，放线程里跑，避免阻塞事件循环
        pieces = await asyncio.to_thread(
            slice_image, data, slice_height=slice_height, overlap=overlap
        )
        if not pieces:
            result.append(url)
            continue

        room = budget - len(result)
        if room <= 1:
            # 只剩一个位置，放弃切片，保留原图信息量更大的整体
            result.append(url)
            continue

        chosen = pieces[:room]
        result.extend(chosen)
        logger.info(
            "[qzone_reader] 长图 %sx%s 切成 %d 片（取 %d 片，剩余额度 %d）",
            size[0],
            size[1],
            len(pieces),
            len(chosen),
            room,
        )
    return result[:budget]
