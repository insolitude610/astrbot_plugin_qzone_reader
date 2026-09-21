"""核心逻辑自测：不依赖 AstrBot / aiohttp，用最小桩模块跑通解析与组装。

用法: python test_core.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
PKG_NAME = "qzone_reader_pkg"

passed = 0
failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed.append(name)
        print(f"  FAIL  {name} {detail}")


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def install_stubs() -> None:
    """注入 astrbot / aiohttp 桩模块，让插件模块可以在裸环境里被导入。"""
    aiohttp = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class ClientError(Exception):
        pass

    class ClientSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    aiohttp.ClientTimeout = ClientTimeout
    aiohttp.ClientError = ClientError
    aiohttp.ClientSession = ClientSession
    sys.modules["aiohttp"] = aiohttp

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


def load_module():
    """以包的形式载入插件，使相对导入可用。"""
    import importlib.util

    pkg = types.ModuleType(PKG_NAME)
    pkg.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[PKG_NAME] = pkg

    core_pkg = types.ModuleType(f"{PKG_NAME}.core")
    core_pkg.__path__ = [str(PLUGIN_DIR / "core")]  # type: ignore[attr-defined]
    sys.modules[f"{PKG_NAME}.core"] = core_pkg

    # qzone_api 会 `from . import frontpage`，main 会 `from .core import images`，
    # 先把同包模块挂上
    fp_spec = importlib.util.spec_from_file_location(
        f"{PKG_NAME}.core.frontpage", PLUGIN_DIR / "core" / "frontpage.py"
    )
    fp_module = importlib.util.module_from_spec(fp_spec)
    sys.modules[fp_spec.name] = fp_module
    setattr(core_pkg, "frontpage", fp_module)
    fp_spec.loader.exec_module(fp_module)

    img_spec = importlib.util.spec_from_file_location(
        f"{PKG_NAME}.core.images", PLUGIN_DIR / "core" / "images.py"
    )
    img_module = importlib.util.module_from_spec(img_spec)
    sys.modules[img_spec.name] = img_module
    setattr(core_pkg, "images", img_module)
    img_spec.loader.exec_module(img_module)

    spec = importlib.util.spec_from_file_location(
        f"{PKG_NAME}.core.qzone_api", PLUGIN_DIR / "core" / "qzone_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.frontpage = fp_module
    module.images = img_module
    return module


def main() -> int:
    install_stubs()
    api = load_module()

    print("\n[1] 从真实形态的 QQ空间卡片 JSON 里提取分享链接")
    # 这是 QQ空间说说分享卡片在 NapCat 里的典型结构
    card = {
        "app": "com.tencent.qzone",
        "view": "text",
        "meta": {
            "detail_1": {
                "appid": 1109993965,
                "title": "看看我的说说",
                "desc": "今天天气不错，出门走了走。",
                "qqdocurl": "https://h5.qzone.qq.com/ugc/share?res_uin=10001&cellid=abc123&t=1700000000",
                "host": {"uin": 10001, "nick": "小明"},
            }
        },
        "prompt": "[QQ空间] 说说",
    }
    url = api.extract_share_url(card)
    check("命中卡片内 qqdocurl", url is not None and "cellid=abc123" in url, repr(url))

    # 嵌套更深的变体（config / extraData 包裹）
    nested = {
        "config": {"forward": True},
        "extraData": {"metaData": {"url": "https://h5.qzone.qq.com/ugc/share?res_uin=20002&cellid=zzz"}},
    }
    check(
        "深层嵌套也能找到",
        (api.extract_share_url(nested) or "").endswith("cellid=zzz"),
        repr(api.extract_share_url(nested)),
    )

    # mobile 短链形态
    mobile = {"meta": {"x": "https://mobile.qzone.qq.com/l?t=1700000000&uin=30003"}}
    check("支持 mobile 短链", "mobile.qzone.qq.com/l" in (api.extract_share_url(mobile) or ""))
    check("与 QQ空间无关的 JSON 返回 None", api.extract_share_url({"a": "https://example.com/x"}) is None)
    check("非 URL 字符串不误判", api.extract_share_url({"a": "qzone.qq.com is a site"}) is None)

    print("\n[2] 解析分享链接参数")
    params = api.parse_share_url(url or "")
    check("取出 res_uin", params.get("res_uin") == "10001", str(params))
    check("取出 cellid", params.get("cellid") == "abc123", str(params))
    check("空链接不报错", api.parse_share_url("not a url") == {})

    print("\n[3] Cookie 解析与 g_tk 推导")
    creds = api.credentials_from_cookie_string(
        "uin=o0012345; skey=@AbCdEf; p_skey=xyz789abc; pt2gguin=o0012345", source="napcat"
    )
    check("解析出 uin", creds is not None and creds.uin == 12345, repr(creds))
    check("解析出 skey/p_skey", creds is not None and creds.p_skey == "xyz789abc")
    check("来源标记正确", creds is not None and creds.source == "napcat")

    # g_tk 必须与社区通行的 djb2 变体一致
    expected = 5381
    for ch in "xyz789abc":
        expected += (expected << 5) + ord(ch)
    expected &= 0x7FFFFFFF
    check("g_tk 计算正确", creds is not None and creds.gtk == str(expected), creds.gtk if creds else "")

    check("缺少 uin 时返回 None", api.credentials_from_cookie_string("skey=a; p_skey=b") is None)
    check("缺少 skey 时返回 None", api.credentials_from_cookie_string("uin=o123") is None)
    check("空字符串返回 None", api.credentials_from_cookie_string("") is None)

    check("uin 不带 o 前缀也能解析", (api.credentials_from_cookie_string("uin=999; p_skey=k") or None) is not None)
    check("p_uin 作为 uin 兜底", (api.credentials_from_cookie_string("p_uin=o888; p_skey=k") or None).uin == 888)

    print("\n[4] jsonp / JSON 解析（QQ空间接口两种返回都要吃）")
    check("jsonp 回调可解析", api._loads_jsonp('_preloadCallback({"msglist":[]});') == {"msglist": []})
    check("裸 JSON 可解析", api._loads_jsonp('{"code":0}') == {"code": 0})
    check("空响应返回 None", api._loads_jsonp("") is None)
    check("脏响应返回 None", api._loads_jsonp("<<html>>") is None)

    print("\n[5] feed -> QzonePost 字段映射")
    feed = {
        "uin": 10001,
        "name": "小明",
        "content": "今天天气不错，出门走了走。",
        "created_time": 1700000000,
        "cmtnum": 3,
        "usenum": 12,
        "source_name": "iPhone",
        "rt_con": {"content": "被转发的原文"},
        "pic": [
            {"url1": "https://a.com/1_small.jpg", "url3": "https://a.com/1_big.jpg"},
            {"url2": "https://a.com/2.jpg"},
            {"not_a_url": 123},
        ],
        "video": {"url1": "https://a.com/v.mp4"},
        "tid": "tid-1",
    }
    post = api._post_from_feed(feed, share_url="https://h5.qzone.qq.com/ugc/share?cellid=abc")
    check("正文映射", post.text == "今天天气不错，出门走了走。", post.text)
    check("作者/uin 映射", post.name == "小明" and post.uin == 10001)
    check("图片取最大尺寸", post.images[0] == "https://a.com/1_big.jpg", str(post.images))
    check("第二张图用 url2", "https://a.com/2.jpg" in post.images, str(post.images))
    check("非 URL 字段被忽略", len(post.images) == 2, str(post.images))
    check("视频被记录", post.videos == ["https://a.com/v.mp4"], str(post.videos))
    check("转发内容映射", post.rt_text == "被转发的原文")
    check("互动数映射", post.like_count == 12 and post.comment_count == 3)
    check("即使用空 feed 也不崩", api._post_from_feed({}, share_url="u").is_empty())

    print("\n[6] 渲染成给模型看的文本")
    prompt = post.to_prompt(max_images=1)
    check("含原文", "今天天气不错" in prompt)
    check("含作者", "小明" in prompt)
    check("含时间", "2023-11-15" in prompt or "2023-11-14" in prompt, prompt[:200])
    check("含配图说明", "共 2 张" in prompt, prompt)
    check("含转发内容", "被转发的原文" in prompt)
    check("max_images=0 时不声称附带图片", "未附带图片内容" in post.to_prompt(max_images=0))

    empty_post = api.QzonePost(text="")
    check("空正文有占位", "没有文字内容" in empty_post.to_prompt())

    print("\n[7] Cookie 缓存 TTL")
    cache = api.CookieCache(ttl=600)
    check("初始为空", cache.get() is None)
    cache.put(creds)
    check("写入后可取", cache.get() is creds)
    cache.clear()
    check("清空后为空", cache.get() is None)
    zero = api.CookieCache(ttl=0)
    zero.put(creds)
    check("ttl=0 表示不缓存但仍可用", zero.get() is not None)

    print("\n[8] 定位分享的那条说说")
    msglist = [
        {"cellid": "other", "created_time": 1699999000},
        {"cellid": "abc123", "created_time": 1700000000},
    ]
    check("优先按 cellid 命中", api._pick_feed(msglist, cell_id="abc123", share_url="")["cellid"] == "abc123")
    by_time = api._pick_feed(
        msglist, cell_id="", share_url="https://h5.qzone.qq.com/ugc/share?t=1699999000"
    )
    check("无 cellid 时按时间戳命中", by_time["cellid"] == "other", str(by_time))
    # 关键回归：定位不到时必须放弃，绝不能随便挑一条
    # （正是这个兜底导致把无关说说注入给了用户）
    check(
        "cellid 对不上时返回 None（不猜）",
        api._pick_feed(msglist, cell_id="nope", share_url="") is None,
    )
    check(
        "无任何定位信息时返回 None（不猜）",
        api._pick_feed(msglist, cell_id="", share_url="https://mobile.qzone.qq.com/l?g=1502&i=x&u=1") is None,
    )
    check("空列表返回 None", api._pick_feed([], cell_id="x", share_url="") is None)

    print("\n[8b] 短链参数识别：u= / i= 不是 QQ 号，不能当 uin 用")
    short = api.parse_share_url(
        "https://mobile.qzone.qq.com/l?g=1502&i=a8cb295f29ebaf6a42240d00&u=1596574632&a=311&sharetag=X"
    )
    check("短链里解析出 i 和 u", short.get("i") == "a8cb295f29ebaf6a42240d00" and short.get("u") == "1596574632")
    check(
        "短链不被当成有定位信息",
        api._has_feed_locator(short) is False,
        str(api._extract_host_uin(short)),
    )
    check("短链推不出 host_uin", api._extract_host_uin(short) is None)

    h5 = api.parse_share_url(
        "https://h5.qzone.qq.com/ugc/share?res_uin=3237747236&cellid=abc&t=1700000000"
    )
    check("h5 分享页能推出 host_uin", api._extract_host_uin(h5) == 3237747236)
    check("h5 分享页被判定有定位信息", api._has_feed_locator(h5) is True)
    check("o 前缀 uin 可解析", api._extract_host_uin({"res_uin": "o3237747236"}) == 3237747236)
    check("位数不合理的 uin 被拒", api._extract_host_uin({"res_uin": "123"}) is None)
    check("非数字 uin 被拒", api._extract_host_uin({"res_uin": "abc"}) is None)
    check("host_uin 字段同样可用", api._extract_host_uin({"host_uin": "10001"}) == 10001)

    print("\n[8c] 安全关键：认不出说说时必须放弃，不能去查自己空间")
    calls = {"msglist": 0}
    real_session = api.aiohttp.ClientSession
    real_msglist = api._fetch_msglist

    class _ResolveOnly:
        """只允许解析短链；一旦有人拉动态列表就说明逻辑跑偏了。"""

        def __init__(self, **kw):
            pass

        def get(self, url, **kw):
            class R:
                url = "https://mobile.qzone.qq.com/l?g=1502&i=x&u=1596574632"
                status = 200

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *e):
                    return False

                async def read(self):
                    return b""

                async def text(self):
                    return ""

            return R()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

    async def counting_msglist(*a, **kw):
        calls["msglist"] += 1
        return [{"cellid": "whatever", "content": "机器人自己空间的第一条"}], 0

    api.aiohttp.ClientSession = _ResolveOnly
    api._fetch_msglist = counting_msglist
    try:
        got = asyncio.run(
            api.fetch_post(
                api.QzoneCredentials(uin=3237747236, skey="s", p_skey="p"),
                "https://mobile.qzone.qq.com/l?g=1502&i=a8cb295f29ebaf6a42240d00&u=1596574632&a=311",
            )
        )
    finally:
        api.aiohttp.ClientSession = real_session
        api._fetch_msglist = real_msglist

    check("短链无法定位时返回 None", got is None, repr(got))
    check("且完全没有去查动态列表（不读自身空间）", calls["msglist"] == 0, str(calls))

    print("\n[8d] 转发说说：必须读到被转发的原文，而不是只剩外层评语")
    fp = api.frontpage
    fixture_path = PLUGIN_DIR / "tests" / "fixtures" / "repost_cell.json"
    check("转发 fixture 存在", fixture_path.exists(), str(fixture_path))
    if fixture_path.exists():
        cell_json = fixture_path.read_text(encoding="utf-8")
        # 复刻真实页面：JS 对象字面量（键带引号、含 // 注释）
        html = (
            "<html><script>var FrontPage = {\n"
            " loginUin : 'NaN', // 页面注释\n"
            ' module : "detail",\n'
            f" data : {cell_json}\n"
            "};</script></html>"
        )
        cell = fp.extract_share_post(html)
        check("能从页面里解出 cell", cell is not None)

        if cell:
            post = api._post_from_cell(cell, url="https://h5.qzone.qq.com/ugc/share/?x=1")
            check("识别为转发", post.is_repost())
            check("外层是转发者的评语", "玩的都比我花" in post.text, post.text)
            check(
                "读到被转发的原文正文",
                len(post.original_text) > 100,
                f"原文长度 {len(post.original_text)}",
            )
            check(
                "原文正文里含瓜条关键内容",
                "野狗聚一窝" in post.original_text,
                post.original_text[:80],
            )
            check("原文作者 uin 正确", post.original_uin == 3484486902, str(post.original_uin))
            check("转发者 uin 正确", post.uin == 1596574632, str(post.uin))
            check("原文发布时间早于转发时间", 0 < post.original_time < post.created_time)
            check(
                "原文配图被读到",
                len(post.original_images) >= 3,
                f"{len(post.original_images)} 张",
            )
            check(
                "图片 URL 是 http 开头",
                all(u.startswith("http") for u in post.original_images),
            )

            rendered = post.to_prompt(max_images=2)
            check("渲染含转发者评语", "玩的都比我花" in rendered)
            check("渲染含原文作者", post.original_name in rendered)
            check("渲染含原文正文", "野狗聚一窝" in rendered)
            check(
                "原文正文不重复出现",
                rendered.count("野狗聚一窝") == 1,
                f"出现 {rendered.count('野狗聚一窝')} 次",
            )
            check("渲染含原文配图说明", "原文配图" in rendered, rendered[-200:])
            check("渲染标注了转发关系", "转发的原内容" in rendered)

            # 非转发不应带原文区块
            plain_cell = {
                "cell_comm": {"time": 1700000000},
                "cell_userinfo": {"user": {"uin": 10001, "nickname": "某人"}},
                "cell_summary": {"summary": "普通说说"},
            }
            plain = api._post_from_cell(plain_cell, url="u")
            check("普通说说不被当成转发", plain is not None and not plain.is_repost())
            check("普通说说渲染无原文区块", "转发的原内容" not in plain.to_prompt())

    print("\n[9] 插件组装逻辑（注入文本 + 图片裁剪）")
    main_mod = load_main_module()
    plugin = make_plugin(main_mod, api, {"summarize_mode": "brief"})

    text = plugin._with_instruction("【QQ空间说说原文】\n正文：\n测试内容", "brief")
    check("brief 模式附带要点总结指令", "请先简要总结" in text and "测试内容" in text)
    plain_text = plugin._with_instruction("BODY", "off")
    check("off 模式只留原文", plain_text == "BODY")

    full_text = plugin._with_instruction("BODY", "full")
    check("full 模式要求完整总结", "完整地总结" in full_text and "BODY" in full_text)
    for kw in ("来龙去脉", "作者的情绪", "配图", "信息不完整"):
        check(f"full 模式覆盖「{kw}」", kw in full_text)
    check(
        "full 不再是「先简要总结」那种聊两句的指令",
        "请先简要总结" not in full_text,
    )
    check(
        "三种模式长度递增",
        len(plain_text) < len(text) < len(full_text),
        f"{len(plain_text)} / {len(text)} / {len(full_text)}",
    )

    # 模式解析：含旧键兼容与非法值回退
    def mode_of(cfg):
        return make_plugin(main_mod, api, cfg)._summarize_mode()

    check("默认（无配置）为 brief", mode_of({}) == "brief")
    check("显式 off", mode_of({"summarize_mode": "off"}) == "off")
    check("显式 full", mode_of({"summarize_mode": "full"}) == "full")
    check("大小写与空格容错", mode_of({"summarize_mode": "  FULL "}) == "full")
    check("非法值回退 brief", mode_of({"summarize_mode": "bogus"}) == "brief")
    check("兼容旧键 auto_summarize=True → brief", mode_of({"auto_summarize": True}) == "brief")
    check("兼容旧键 auto_summarize=False → off", mode_of({"auto_summarize": False}) == "off")
    check(
        "新键优先于旧键",
        mode_of({"summarize_mode": "full", "auto_summarize": False}) == "full",
    )

    # extra_user_content_parts 注入：没有 ContentPart 类时应退回 dict
    class FakeReq:
        def __init__(self):
            self.extra_user_content_parts = []
            self.image_urls = []

    req = FakeReq()
    asyncio.run(plugin.inject_qzone_content(object(), req))
    check("没有任务时不注入", req.extra_user_content_parts == [])

    class Ev:
        pass

    ev = Ev()
    plugin._pending[id(ev)] = ("注入文本", ["https://img/1.jpg"])
    req2 = FakeReq()
    asyncio.run(plugin.inject_qzone_content(ev, req2))
    check("文本注入到 extra_user_content_parts", len(req2.extra_user_content_parts) == 1)
    check("图片注入到 image_urls", req2.image_urls == ["https://img/1.jpg"])
    check("注入后清理暂存", id(ev) not in plugin._pending)

    print("\n[10] 会话白名单")
    plugin2 = make_plugin(main_mod, api, {"group_whitelist": []})

    class E2:
        def get_group_id(self):
            return "111"

        def get_sender_id(self):
            return "222"

    plugin2.config = {"group_whitelist": []}
    check("白名单为空时全部生效", plugin2._in_scope(E2()) is True)
    plugin2.config = {"group_whitelist": ["111"]}
    check("命中群号生效", plugin2._in_scope(E2()) is True)
    plugin2.config = {"group_whitelist": ["999"]}
    check("未命中则不生效", plugin2._in_scope(E2()) is False)
    print("\n[11] 卡片识别（Json 组件 + 纯文本 + raw 兜底）")
    from types import SimpleNamespace

    class FakeEvent:
        def __init__(self, chain, text="", raw=None):
            self.message_obj = SimpleNamespace(message=chain, raw_message=raw)
            self.message_str = text

    json_comp = main_mod.Json(card)
    plugin3 = make_plugin(main_mod, api, {})
    found = plugin3._find_share_url(FakeEvent([json_comp]))
    check("从 Json 组件找到链接", found is not None and "cellid=abc123" in found, str(found))

    found_text = plugin3._find_share_url(
        FakeEvent([main_mod.Plain(text="看看 https://h5.qzone.qq.com/ugc/share?res_uin=1&cellid=txt")])
    )
    check("从纯文本找到链接", found_text is not None and "cellid=txt" in found_text, str(found_text))

    found_raw = plugin3._find_share_url(
        FakeEvent([], raw={"post_type": "message", "message": [{"type": "json", "data": card}]})
    )
    check("从 raw_message 兜底找到链接", found_raw is not None and "cellid=abc123" in found_raw, str(found_raw))

    check("普通聊天消息不触发", plugin3._find_share_url(FakeEvent([main_mod.Plain(text="你好呀")])) is None)

    print("\n[12] 卡片字段的转义还原与降级文案")
    # QQ 卡片会把逗号转义成 &#44;，不还原会把 query 参数截断
    escaped = {
        "meta": {
            "detail_1": {
                "title": "我的说说",
                "desc": "今天出门走了走",
                "qqdocurl": "https://h5.qzone.qq.com/ugc/share?res_uin=10001&#44;cellid=esc1&#44;t=1700000000",
            }
        }
    }
    esc_url = api.extract_share_url(escaped) or ""
    check("还原 &#44; 后完整取出 URL", "cellid=esc1" in esc_url and "t=1700000000" in esc_url, esc_url)
    check("还原后链接被判定为分享链接", api._looks_like_share(esc_url), esc_url)
    check("还原后参数可解析", api.parse_share_url(esc_url).get("cellid") == "esc1")

    card_text = api.extract_card_text(escaped)
    check("降级文案含标题", "我的说说" in card_text, card_text)
    check("降级文案含摘要", "今天出门走了走" in card_text, card_text)
    check("无文案字段时返回空", api.extract_card_text({"a": 1}) == "")
    check(
        "降级文案不接受超长正文",
        api.extract_card_text({"title": "x" * 600}) == "",
    )

    print("\n[13] 平台门禁（on_llm_request 不经过 event_filters，必须手动判断）")

    class PlatEvent:
        def __init__(self, name):
            self._name = name

        def get_platform_name(self):
            return self._name

    check("aiocqhttp 放行", plugin3._platform_ok(PlatEvent("aiocqhttp")) is True)
    check("其他平台拦截", plugin3._platform_ok(PlatEvent("telegram")) is False)

    class BadEvent:
        def get_platform_name(self):
            raise RuntimeError("no platform")

    check("取平台名异常时放行（不阻断对话）", plugin3._platform_ok(BadEvent()) is True)

    # 非 aiocqhttp 会话即使有待注入内容也不应注入
    plugin4 = make_plugin(main_mod, api, {})
    pev = PlatEvent("telegram")
    plugin4._pending[id(pev)] = ("不该注入", [])
    req4 = FakeReq()
    asyncio.run(plugin4.inject_qzone_content(pev, req4))
    check("非 aiocqhttp 不注入", req4.extra_user_content_parts == [])
    check("非 aiocqhttp 也会清掉暂存", id(pev) not in plugin4._pending)

    print("\n[14] 登录态失效检测（缓存要能被作废）")
    check("QzoneAuthError 是 RuntimeError", issubclass(api.QzoneAuthError, RuntimeError))
    check("失效码被收录", -3000 in api.AUTH_ERROR_CODES)

    # 用假 session 让 fetch_post 走到解析分支，验证失效码会抛出
    class FakeResp:
        def __init__(self, body: bytes, url: str, status=200):
            self._body = body
            self.url = url  # 真实响应有 url，_resolve_share 依赖它
            self.status = status

        async def read(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def __init__(self, body: bytes):
            self._body = body

        def get(self, url, params=None, headers=None, **kw):
            # 真实调用会传 allow_redirects 等参数，桩必须一并接受
            return FakeResp(self._body, url)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    real_session = api.aiohttp.ClientSession

    async def run_with(body: bytes):
        api.aiohttp.ClientSession = lambda **kw: FakeSession(body)
        try:
            return await api.fetch_post(
                api.QzoneCredentials(uin=10001, skey="s", p_skey="p"),
                "https://h5.qzone.qq.com/ugc/share?res_uin=10001&cellid=c",
            )
        finally:
            api.aiohttp.ClientSession = real_session

    try:
        asyncio.run(run_with(b'_preloadCallback({"code":-3000,"msg":"login"});'))
        check("失效码应抛出 QzoneAuthError", False, "没有抛出")
    except api.QzoneAuthError:
        check("失效码抛出 QzoneAuthError", True)
    except Exception as exc:  # noqa: BLE001
        check("失效码抛出 QzoneAuthError", False, f"抛出了 {type(exc).__name__}")

    # 正常返回则不应抛错
    normal = asyncio.run(
        run_with(
            b'_preloadCallback({"code":0,"msglist":[{"cellid":"c","content":"hi","uin":1}]});'
        )
    )
    check("正常返回可解析出正文", normal is not None and normal.text == "hi", repr(normal))

    # 空 msglist 返回 None 而不是抛错
    empty = asyncio.run(run_with(b'_preloadCallback({"code":0,"msglist":[]});'))
    check("空动态列表返回 None", empty is None)

    print("\n[15] 登录态失效后自动重取一次")
    calls = {"fetch": 0, "creds": 0}

    async def fake_fetch(creds, url):
        calls["fetch"] += 1
        if calls["fetch"] == 1:
            raise api.QzoneAuthError("expired")
        return api.QzonePost(text="重取后成功")

    class P:
        pass

    p = make_plugin(main_mod, api, {})
    p.cookies.put(api.QzoneCredentials(uin=1, skey="old", p_skey="old"))

    async def fake_get_credentials(event):
        calls["creds"] += 1
        return api.QzoneCredentials(uin=1, skey="new", p_skey="new")

    p._get_credentials = fake_get_credentials
    original_fetch = main_mod.fetch_post
    main_mod.fetch_post = fake_fetch
    try:
        result = asyncio.run(p._fetch_with_retry(P(), api.QzoneCredentials(uin=1, skey="o", p_skey="o"), "u"))
    finally:
        main_mod.fetch_post = original_fetch

    check("第一次失效后重取了登录态", calls["creds"] == 1, str(calls))
    check("重试后拿到内容", result is not None and result.text == "重取后成功", repr(result))
    check("重试共调用两次接口", calls["fetch"] == 2, str(calls))

    # 非失效异常不应触发重取
    calls2 = {"fetch": 0, "creds": 0}

    async def boom_fetch(creds, url):
        calls2["fetch"] += 1
        raise ValueError("network down")

    async def creds2(event):
        calls2["creds"] += 1
        return api.QzoneCredentials(uin=1, skey="n", p_skey="n")

    p2 = make_plugin(main_mod, api, {})
    p2._get_credentials = creds2
    main_mod.fetch_post = boom_fetch
    try:
        result2 = asyncio.run(p2._fetch_with_retry(P(), api.QzoneCredentials(uin=1, skey="o", p_skey="o"), "u"))
    finally:
        main_mod.fetch_post = original_fetch

    check("普通异常不重取登录态", calls2["creds"] == 0, str(calls2))
    check("普通异常返回 None（走降级）", result2 is None)

    print("\n[16] 长截图切片（瓜条配图读不出文字的根因）")
    iu = api.images
    try:
        from PIL import Image as _PILImage

        has_pil = True
    except Exception:  # noqa: BLE001
        _PILImage = None
        has_pil = False
    check("Pillow 可用（切片依赖）", has_pil)

    if has_pil:
        import base64 as _b64
        import io as _io

        def make_image(w, h, fmt="JPEG", quality=85):
            im = _PILImage.new("RGB", (w, h), (255, 255, 255))
            # 画一些横线，模拟聊天文字行
            for y in range(0, h, 50):
                for x in range(8, max(w - 8, 9), 3):
                    im.putpixel((x, y), (20, 20, 20))
            buf = _io.BytesIO()
            im.save(buf, format=fmt, quality=quality)
            return buf.getvalue()

        def decode_size(data_url):
            raw = _b64.b64decode(data_url.split(",", 1)[1])
            with _PILImage.open(_io.BytesIO(raw)) as s:
                return s.size

        check("长截图判定：640x3799", iu.is_tall(640, 3799) is True)
        check("长截图判定：476x4096", iu.is_tall(476, 4096) is True)
        check("普通图判定：1200x900", iu.is_tall(1200, 900) is False)
        # 1400x1700：高度过阈值，但宽高比 1.21 未到 1.5，属于正常竖图，不该切
        check("竖图但宽高比不足不切：1400x1700", iu.is_tall(1400, 1700) is False)
        check("刚过阈值的高瘦图要切：640x1600", iu.is_tall(640, 1600) is True)

        tall = make_image(640, 3799)
        pieces = iu.slice_image(tall)
        check("真实尺寸长图被切片", len(pieces) >= 3, f"{len(pieces)} 片")
        check("切片是 data URL", all(p.startswith("data:image/jpeg;base64,") for p in pieces))
        sizes = [decode_size(p) for p in pieces]
        check("切片保持原宽度", all(w == 640 for w, _ in sizes), str(sizes))
        check("每片高度不超过目标", all(h <= iu.DEFAULT_SLICE_HEIGHT for _, h in sizes), str(sizes))
        check(
            "切片能覆盖整图高度（含重叠）",
            sum(h for _, h in sizes) >= 3799,
            f"合计 {sum(h for _, h in sizes)}",
        )
        check("切片不重叠丢失：末片到底部", sizes[-1][1] <= iu.DEFAULT_SLICE_HEIGHT)

        normal = make_image(1200, 900)
        check("普通图不切片", iu.slice_image(normal) == [])

        extreme = make_image(600, 12000, quality=60)
        extreme_pieces = iu.slice_image(extreme)
        check(
            "极端长图受单图片数上限约束",
            len(extreme_pieces) <= iu.MAX_SLICES_PER_IMAGE,
            f"{len(extreme_pieces)} 片",
        )

        check("损坏数据返回空列表（不抛错）", iu.slice_image(b"not an image") == [])
        check("空数据返回空列表", iu.slice_image(b"") == [])

        # prepare_images：预算与切片的关系
        class _Resp:
            def __init__(self, body):
                self._body = body
                self.status = 200  # fetch_bytes 会读 status，桩必须提供

            async def read(self):
                return self._body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False

        class _Session:
            def __init__(self, body):
                self._body = body
                self.calls = 0

            def get(self, url, **kw):
                self.calls += 1
                return _Resp(self._body)

        sess = _Session(tall)
        out = asyncio.run(
            iu.prepare_images(sess, ["http://x/1.jpg"], budget=4, slice_tall=True)
        )
        check("预算限制生效：最多 4 张", len(out) <= 4, f"{len(out)} 张")
        check("切片占用了预算额度", 1 < len(out) <= 4, f"{len(out)} 张")

        sess2 = _Session(tall)
        out2 = asyncio.run(
            iu.prepare_images(sess2, ["http://x/1.jpg"], budget=0, slice_tall=True)
        )
        check("预算为 0 返回空", out2 == [])

        sess3 = _Session(normal)
        out3 = asyncio.run(
            iu.prepare_images(sess3, ["http://x/n.jpg"], budget=4, slice_tall=True)
        )
        check("普通图原样返回 URL", out3 == ["http://x/n.jpg"], str(out3))

        sess4 = _Session(b"broken")
        out4 = asyncio.run(
            iu.prepare_images(sess4, ["http://x/b.jpg"], budget=4, slice_tall=True)
        )
        check("坏图退回原 URL 而不是丢弃", out4 == ["http://x/b.jpg"], str(out4))

        sess5 = _Session(tall)
        out5 = asyncio.run(
            iu.prepare_images(sess5, ["http://x/1.jpg"], budget=4, slice_tall=False)
        )
        check("关闭切片时直接给原 URL", out5 == ["http://x/1.jpg"], str(out5))

    print("\n[17] 配置项接线")
    p = make_plugin(main_mod, api, {})
    check("默认开启长图切片", bool(p.config.get("slice_tall_images", True)) is True)
    check(
        "切片高度留 0 时回退默认值",
        (lambda v: (v if v > 0 else api.images.DEFAULT_SLICE_HEIGHT))(
            int(p.config.get("slice_max_height", 0) or 0)
        )
        == api.images.DEFAULT_SLICE_HEIGHT,
    )
    check("已移除隐藏硬上限 HARD_IMAGE_CAP", not hasattr(main_mod, "HARD_IMAGE_CAP"))

    print("\n[18] 上下文保留模式 context_mode")
    from astrbot.core.agent.message import TextPart as _TextPart, ImageURLPart as _ImgPart

    p_keep = make_plugin(main_mod, api, {"context_mode": "keep"})
    p_once = make_plugin(main_mod, api, {"context_mode": "once"})
    p_default = make_plugin(main_mod, api, {})

    check("keep 模式保留上下文", p_keep._keep_in_context() is True)
    check("once 模式不保留上下文", p_once._keep_in_context() is False)
    check("默认（未配置）为保留", p_default._keep_in_context() is True)
    check(
        "大小写与空格容错",
        make_plugin(main_mod, api, {"context_mode": "  ONCE "})._keep_in_context() is False,
    )
    check(
        "非法值回退为 keep",
        make_plugin(main_mod, api, {"context_mode": "bogus"})._keep_in_context() is True,
    )

    class _Req:
        def __init__(self):
            self.extra_user_content_parts = []
            self.image_urls = []

    # keep：持久 part（不带 _no_save），图片走 image_urls
    r1 = _Req()
    main_mod.QzoneReaderPlugin._append_extra_part(r1, "说说原文", persist=True)
    check("keep：文本 part 可持久化", r1.extra_user_content_parts[0]._no_save is False)

    # once：临时 part，落历史时会被过滤
    r2 = _Req()
    main_mod.QzoneReaderPlugin._append_extra_part(r2, "说说原文", persist=False)
    check("once：文本 part 标记临时", r2.extra_user_content_parts[0]._no_save is True)
    check(
        "临时 part 的 dump 带 _no_save",
        r2.extra_user_content_parts[0].model_dump().get("_no_save") is True,
    )

    main_mod.QzoneReaderPlugin._append_image_part(r2, "https://x/1.jpg")
    check("once：图片进入 extra_user_content_parts", len(r2.extra_user_content_parts) == 2)
    img_part = r2.extra_user_content_parts[1]
    check("once：图片 part 类型为 image_url", getattr(img_part, "type", "") == "image_url")
    check("once：图片 part 标记临时", img_part._no_save is True)
    check(
        "once：图片 url 正确",
        img_part.image_url.get("url") == "https://x/1.jpg",
        str(img_part.image_url),
    )
    check("once：不动用 req.image_urls（那个字段会落库）", r2.image_urls == [])

    # 端到端：模拟落历史时的过滤（复刻 message.py 的 dump 逻辑）
    def dump_saved(parts):
        return [p.model_dump() for p in parts if not getattr(p, "_no_save", False)]

    kept = dump_saved(r1.extra_user_content_parts)
    dropped = dump_saved(r2.extra_user_content_parts)
    check("keep：落历史保留说说文本", any("说说原文" in str(x.get("text", "")) for x in kept))
    check("once：落历史不保留说说文本", not any("说说原文" in str(x.get("text", "")) for x in dropped))
    check("once：落历史不保留图片", not any(x.get("type") == "image_url" for x in dropped))
    check("once：落历史后确实清空", dropped == [], str(dropped))

    print("\n[19] 注入流程按 context_mode 分流")
    plugin_m = make_plugin(main_mod, api, {"context_mode": "keep"})

    class _Ev:
        pass

    ev_keep = _Ev()
    plugin_m._pending[id(ev_keep)] = ("说说文本", ["https://x/a.jpg", "https://x/b.jpg"])
    req_k = _Req()
    asyncio.run(plugin_m.inject_qzone_content(ev_keep, req_k))
    check("keep：图片进 req.image_urls", len(req_k.image_urls) == 2, str(req_k.image_urls))
    check("keep：图片不进 extra parts", len(req_k.extra_user_content_parts) == 1)

    plugin_o = make_plugin(main_mod, api, {"context_mode": "once"})
    ev_once = _Ev()
    plugin_o._pending[id(ev_once)] = ("说说文本", ["https://x/a.jpg", "https://x/b.jpg"])
    req_o = _Req()
    asyncio.run(plugin_o.inject_qzone_content(ev_once, req_o))
    check("once：图片不进 req.image_urls", req_o.image_urls == [], str(req_o.image_urls))
    check(
        "once：文本 + 两张图共 3 个临时 part",
        len(req_o.extra_user_content_parts) == 3,
        str(len(req_o.extra_user_content_parts)),
    )
    check(
        "once：全部 part 都是临时的",
        all(getattr(x, "_no_save", False) for x in req_o.extra_user_content_parts),
    )

    print("\n[20] 群聊：先发卡片、再引用并 @bot")
    # 真实的群聊序列：msg1 是卡片分享；msg2 引用 msg1 并 @bot，
    # AstrBot 会把被引用消息抓回来放进 Reply.chain（get_reply 默认 True）
    card_json = main_mod.Json(
        {
            "app": "com.tencent.qzone",
            "meta": {
                "detail_1": {
                    "title": "看看我的说说",
                    "desc": "今天天气不错",
                    "qqdocurl": "https://h5.qzone.qq.com/ugc/share?res_uin=10001&cellid=quoted1",
                }
            },
        }
    )
    quoted = main_mod.Reply(
        id="1001",
        chain=[card_json, main_mod.Plain(text="看看我的说说")],
        sender_id="999",
        sender_nickname="群友",
        message_str="看看我的说说",
    )
    # 第二条消息：At(bot) + Reply(卡片)
    class At:
        def __init__(self, qq=""):
            self.qq = qq

    ev_quoted = FakeEvent([At(qq="3237747236"), quoted, main_mod.Plain(text=" 这瓜啥情况")])

    found = plugin3._find_share_url(ev_quoted)
    check(
        "能从被引用消息里认出卡片链接",
        found is not None and "cellid=quoted1" in found,
        str(found),
    )
    check(
        "字段缺失时不误报：被引用的是纯文本",
        plugin3._find_share_url(FakeEvent([main_mod.Reply(chain=[main_mod.Plain(text="普通聊天")])]))
        is None,
    )

    card_text = plugin3._card_text(ev_quoted)
    check("降级文案含被引用卡片的标题", "看看我的说说" in card_text, card_text)
    check("降级文案含用户自己说的话", "这瓜啥情况" in card_text, card_text)

    # 引用里再套引用：应能递归找到，且不会无限递归
    nested = main_mod.Reply(id="2", chain=[main_mod.Reply(id="1", chain=[card_json])])
    nested_found = plugin3._find_share_url(FakeEvent([nested]))
    check(
        "嵌套引用也能找到",
        nested_found is not None and "cellid=quoted1" in nested_found,
        str(nested_found),
    )

    self_ref = main_mod.Reply(id="3")
    self_ref.chain = [self_ref]  # 自引用，故意构造死循环
    check(
        "自引用不死循环（depth 保护）",
        plugin3._find_share_url(FakeEvent([self_ref])) is None,
    )

    # 顶层卡片仍优先于引用内容
    top_card = main_mod.Json(
        {"meta": {"detail_1": {"qqdocurl": "https://h5.qzone.qq.com/ugc/share?res_uin=2&cellid=top1"}}}
    )
    mixed = plugin3._find_share_url(FakeEvent([top_card, quoted]))
    check("顶层卡片优先", mixed is not None and "cellid=top1" in mixed, str(mixed))

    print("\n[21] 唤醒门禁：未 @bot 的群消息不该触发抓取")
    # AstrBot 用 event.is_at_or_wake_command 决定要不要走 LLM 链路
    # （process_stage/stage.py:58），默认 False。插件必须提前看这个标志，
    # 否则群里一张没人 @bot 的卡片也会触发全量抓取+切片。
    pw = make_plugin(main_mod, api, {})

    class _Ev:
        pass

    _MISSING = object()

    def ev_with(value):
        e = _Ev()
        if value is not _MISSING:
            e.is_at_or_wake_command = value
        return e

    check("被 @ 时放行", pw._will_wake_bot(ev_with(True)) is True)
    check("未被 @ 时拦截", pw._will_wake_bot(ev_with(False)) is False)
    check("属性缺失时保守放行", pw._will_wake_bot(ev_with(_MISSING)) is True)
    e_none = _Ev()
    e_none.is_at_or_wake_command = None
    check("属性为 None 时保守放行", pw._will_wake_bot(e_none) is True)

    # 端到端：未被唤醒时 capture_qzone_share 必须完全不触发任何抓取
    calls = {"build": 0, "find": 0}
    real_build = pw._build_payload
    real_find = pw._find_share_url

    async def fake_build(event, url):
        calls["build"] += 1
        return ("不该被调用", [])

    def fake_find(event):
        calls["find"] += 1
        return "https://h5.qzone.qq.com/ugc/share?res_uin=1&cellid=x"

    pw._build_payload = fake_build
    pw._find_share_url = fake_find
    try:
        ev_silent = ev_with(False)
        asyncio.run(pw.capture_qzone_share(ev_silent))
        check("未唤醒时不做任何抓取", calls["build"] == 0, str(calls))
        check("未唤醒时连链接都不解析", calls["find"] == 0, str(calls))
        check("未唤醒时不产生待注入内容", id(ev_silent) not in pw._pending)

        calls["build"] = calls["find"] = 0
        ev_woke = ev_with(True)
        asyncio.run(pw.capture_qzone_share(ev_woke))
        check("被唤醒时正常抓取", calls["build"] == 1, str(calls))
        check("被唤醒时正常产生待注入内容", id(ev_woke) in pw._pending)
    finally:
        pw._build_payload = real_build
        pw._find_share_url = real_find

    print("\n" + "=" * 56)
    print(f"通过 {passed} 项，失败 {len(failed)} 项")
    if failed:
        for name in failed:
            print(f"  - {name}")
        return 1
    print("全部通过 ✅")
    return 0


def make_plugin(main_mod, api, config: dict):
    """构造一个真实初始化的插件实例（不能绕过 __init__，否则状态缺失）。"""
    plugin = main_mod.QzoneReaderPlugin(context=None, config=config)
    if not isinstance(getattr(plugin, "cookies", None), api.CookieCache):
        plugin.cookies = api.CookieCache(ttl=600)
    return plugin


def load_main_module():
    """载入 main.py（stub 掉 astrbot.api.event / provider / star）。"""
    import importlib.util

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # noqa: D401 - 测试桩
        pass

    class _Filter:
        class EventMessageType:
            ALL = "all"

        @staticmethod
        def event_message_type(*_a, **_k):
            def deco(fn):
                return fn

            return deco

        @staticmethod
        def on_llm_request(*_a, **_k):
            def deco(fn):
                return fn

            return deco

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter
    sys.modules["astrbot.api.event"] = event_mod

    provider_mod = types.ModuleType("astrbot.api.provider")

    class ProviderRequest:
        pass

    provider_mod.ProviderRequest = ProviderRequest
    sys.modules["astrbot.api.provider"] = provider_mod

    star_mod = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None, config=None):
            self.context = context

    star_mod.Context = Context
    star_mod.Star = Star
    sys.modules["astrbot.api.star"] = star_mod

    components_mod = types.ModuleType("astrbot.core.message.components")

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Json:
        def __init__(self, data=None):
            if isinstance(data, str):
                data = json.loads(data)
            self.data = data

    components_mod.Plain = Plain
    components_mod.Json = Json

    class Reply:
        """桩：模拟 AstrBot 的 Reply，chain 里是被引用消息的消息链。"""

        def __init__(self, id="1", chain=None, sender_id="", sender_nickname="",
                     message_str="", **kw):
            self.id = id
            self.chain = chain or []
            self.sender_id = sender_id
            self.sender_nickname = sender_nickname
            self.message_str = message_str
            self.text = message_str

    components_mod.Reply = Reply
    sys.modules["astrbot.core.message.components"] = components_mod

    # 桩：模拟 astrbot.core.agent.message 的 TextPart / ImageURLPart
    # 真实类支持 mark_as_temp() 置 _no_save，落历史时会被过滤
    agent_msg_mod = types.ModuleType("astrbot.core.agent.message")

    class _ContentPart:
        def __init__(self, **kw):
            self._no_save = False
            for k, v in kw.items():
                setattr(self, k, v)

        def mark_as_temp(self):
            self._no_save = True
            return self

        def model_dump(self):
            data = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
            if self._no_save:
                data["_no_save"] = True
            return data

    class TextPart(_ContentPart):
        type = "text"

        def __init__(self, text=""):
            super().__init__(text=text)

    class ImageURLPart(_ContentPart):
        type = "image_url"

        def __init__(self, image_url=None):
            if not isinstance(image_url, dict):
                raise ValueError("image_url 必须是 dict")
            super().__init__(image_url=image_url)

    agent_msg_mod.TextPart = TextPart
    agent_msg_mod.ImageURLPart = ImageURLPart
    # from astrbot.core.agent.message import X 需要父子包都在 sys.modules 里
    agent_pkg = types.ModuleType("astrbot.core.agent")
    agent_pkg.__path__ = []  # type: ignore[attr-defined]
    agent_pkg.message = agent_msg_mod  # type: ignore[attr-defined]
    core_pkg_stub = types.ModuleType("astrbot.core")
    core_pkg_stub.__path__ = []  # type: ignore[attr-defined]
    core_pkg_stub.agent = agent_pkg  # type: ignore[attr-defined]
    sys.modules["astrbot.core"] = core_pkg_stub
    sys.modules["astrbot.core.agent"] = agent_pkg
    sys.modules["astrbot.core.agent.message"] = agent_msg_mod

    core_mod = types.ModuleType("astrbot.core")
    core_mod.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("astrbot.core", core_mod)
    msg_mod = types.ModuleType("astrbot.core.message")
    msg_mod.__path__ = []  # type: ignore[attr-defined]
    sys.modules.setdefault("astrbot.core.message", msg_mod)

    import importlib.util as ilu

    spec = ilu.spec_from_file_location(f"{PKG_NAME}.main", PLUGIN_DIR / "main.py")
    module = ilu.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    raise SystemExit(main())
