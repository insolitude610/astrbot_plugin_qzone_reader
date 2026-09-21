"""文档一致性检查。

核对 README.md / DEVELOPMENT.md 的关键声明与真实代码是否一致，
防止改了代码忘了改文档。只读，不修改任何文件。

用法:
    python scripts/check_docs.py          # 在插件根目录执行
    python scripts/check_docs.py <插件根目录>

退出码 0 表示一致，1 表示发现问题。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

BT = chr(96)  # 反引号

# README 配置表里允许的「人类可读」默认值写法（与 schema 值的等价映射）
DEFAULT_ALIASES: dict[str, set[str]] = {
    "true": {"true", "True", "开"},
    "false": {"false", "False", "关"},
    "0": {"0"},
    "": {"空", '""', "''"},
    "[]": {"空", "[]", "留空"},
}


def normalize(value: str) -> str:
    """归一化文档里的默认值写法。

    README 里 `"napcat"` 表示「这是个字符串」、`true` 表示 JSON 布尔字面量，
    都是正确的文档表达，不应判为不一致。这里统一去掉包裹引号并小写化。
    """
    text = value.strip()
    for quote in ('"', "'", BT):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            text = text[1:-1].strip()
            break
    return text


def equivalent(doc_value: str, schema_value: str) -> bool:
    """判断文档里写的默认值与 schema 的值是否等价。"""
    doc = normalize(doc_value)
    expected = normalize(schema_value)
    if doc == expected:
        return True
    if doc.lower() == expected.lower():
        return True
    return doc in DEFAULT_ALIASES.get(expected, set())


def parse_table(text: str) -> dict[str, str]:
    """把 markdown 表格解析成 {第一列: 第二列}。"""
    rows: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        key = cells[0].strip(BT).strip()
        if not key or set(key) <= set("-: "):
            continue
        rows.setdefault(key, cells[1].strip(BT).strip())
    return rows


def collect_log_texts(root: pathlib.Path) -> set[str]:
    """收集代码里所有 [qzone_reader] 日志文案（占位符已剥离）。"""
    code = "".join(
        (root / p).read_text(encoding="utf-8")
        for p in ("main.py", "core/qzone_api.py", "core/images.py", "core/frontpage.py")
    )
    out: set[str] = set()
    for match in re.finditer(r"\[qzone_reader\]([^\"'\n]*)", code):
        text = re.sub(r"%[sd]", "", match.group(1)).strip()
        if text:
            out.add(text)
    return out


def run_test_count(root: pathlib.Path) -> int | None:
    """实际跑一次 test_core.py，从输出里取真实断言数。

    静态数 `check(` 会把字符串/提示语里的同名文本也算进去，不准。
    实测：某次静态数 213，真实数 216 —— 差的就是 f-string 里的字面量。
    """
    import subprocess
    import sys

    script = root / "test_core.py"
    if not script.exists():
        return None
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(root),
            timeout=300,
        )
    except Exception:
        return None
    match = re.search(r"通过 (\d+) 项", proc.stdout or "")
    return int(match.group(1)) if match else None


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    readme = (root / "README.md").read_text(encoding="utf-8")
    dev = (root / "DEVELOPMENT.md").read_text(encoding="utf-8")
    main_src = (root / "main.py").read_text(encoding="utf-8")
    test_src = (root / "test_core.py").read_text(encoding="utf-8")

    problems: list[str] = []
    logs = collect_log_texts(root)

    def section(title: str) -> None:
        print()
        print("=" * 64)
        print(title)
        print("=" * 64)

    section("1. README 配置表 vs _conf_schema.json")
    rows = parse_table(readme)
    for key, spec in schema.items():
        default = str(spec.get("default"))
        doc = rows.get(key)
        if doc is None:
            problems.append(f"README 配置表缺少 `{key}`")
            print(f"  缺失    {key}")
        elif not equivalent(doc, default):
            problems.append(f"README 里 {key} 默认值写作 {doc}，schema 是 {default}")
            print(f"  不一致  {key}: README={doc} schema={default}")
        else:
            print(f"  OK      {key} = {default}")

    section("2. schema 里的键是否都被代码读取")
    for key in schema:
        if f'"{key}"' in main_src:
            print(f"  OK      {key}")
        else:
            problems.append(f"配置项 {key} 在 schema 里但 main.py 从未读取")
            print(f"  未使用  {key}")

    section("3. 文档断言数 vs 实际测试数")
    match = re.search(r"当前 \*\*(\d+) 项断言\*\*", dev)
    if not match:
        problems.append("DEVELOPMENT.md 里找不到断言数声明")
        print("  未声明")
    else:
        claimed = int(match.group(1))
        actual = run_test_count(root)
        if actual is None:
            # 跑不起来就退回静态统计，但说明这是近似值
            approx = len(re.findall(r"^\s*check\(", test_src, re.M))
            print(f"  文档声称 {claimed}；测试跑不起来，静态统计约 {approx} 处")
            print("  （静态统计会把字符串里的 check( 也算进去，仅供参考）")
        else:
            print(f"  文档声称 {claimed}，实际运行 {actual} 项")
            if claimed != actual:
                problems.append(f"断言数不一致：文档 {claimed}，实际 {actual}")
                print("  不一致（改测试后请同步更新 DEVELOPMENT.md）")
            else:
                print("  OK")

    section("4. 文档引用的日志串是否与代码一致")
    referenced = 0
    for name, text in (("README", readme), ("DEVELOPMENT", dev)):
        # 只在单行内匹配，避免跨行把整段正文当成一个片段
        for frag in re.findall(BT + "([^" + BT + r"\n]+)" + BT, text):
            frag = frag.strip()
            if not any(k in frag for k in ("qzone_reader", "放弃读取", "分享页没取到")):
                continue
            if "[" in frag:
                frag = frag.split("]", 1)[-1].strip()
            # 文档里的 <占位符> 换成通配再比对
            pattern = re.escape(frag).replace(r"\<", "<").replace(r"\>", ">")
            pattern = re.sub(r"<[^>]+>", ".+", pattern)
            referenced += 1
            if any(re.search(pattern, t) or t in frag for t in logs):
                print(f"  OK      {name}: {frag}")
            else:
                problems.append(f"{name} 引用的日志串在代码里找不到：{frag}")
                print(f"  对不上  {name}: {frag}")
    if referenced == 0:
        print("  （文档未直接引用日志串）")

    section("5. 是否残留已删除的概念（被当作现存常量/配置使用）")
    # 只查「定义或赋值」形式，历史注记里提到旧名是合理的，不应误报
    stale = {
        r"HARD_IMAGE_CAP\s*=": "已移除的图片硬上限常量被重新定义",
        r"^\s*SUMMARIZE_INSTRUCTION\s*=": "已改名为 BRIEF_SUMMARY_INSTRUCTION",
        r"SolitudeRA/astrbot_plugin_qzone_reader": "错误的仓库地址",
    }
    found = False
    for pattern, why in stale.items():
        for name, text in (("README.md", readme), ("DEVELOPMENT.md", dev)):
            if re.search(pattern, text, re.M):
                found = True
                problems.append(f"{name} 仍在使用 {pattern}（{why}）")
                print(f"  残留    {name}: {pattern}  ({why})")
    if not found:
        print("  无残留（历史注记不算）")

    section("5b. 历史注记是否明确标注为已移除")
    # 提到旧名时必须同时出现「已移除 / 早期版本 / 已删除」这类说明，否则会误导读者
    for term in ("HARD_IMAGE_CAP",):
        for name, text in (("README.md", readme), ("DEVELOPMENT.md", dev)):
            if term not in text:
                continue
            windows = [
                text[max(0, m.start() - 120) : m.start() + 120]
                for m in re.finditer(re.escape(term), text)
            ]
            ok = all(
                any(k in w for k in ("移除", "删除", "早期版本", "历史")) for w in windows
            )
            if ok:
                print(f"  OK      {name} 提到 {term} 时标注了已移除")
            else:
                problems.append(f"{name} 提到 {term} 但没有说明它已被移除，易误导")
                print(f"  易误导  {name}: {term} 缺少「已移除」说明")

    section("6. metadata.yaml 与 README 的仓库地址一致")
    meta = (root / "metadata.yaml").read_text(encoding="utf-8")
    m = re.search(r"^repo:\s*(\S+)", meta, re.M)
    if not m:
        problems.append("metadata.yaml 缺少 repo 字段")
        print("  metadata.yaml 缺少 repo")
    else:
        url = m.group(1)
        print(f"  metadata repo = {url}")
        if url in readme:
            print("  OK      README 里出现同一地址")
        else:
            print("  README 未引用该地址（不一定算问题）")

    print()
    print("=" * 64)
    if problems:
        print(f"发现 {len(problems)} 处不一致：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("文档与代码一致 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
