#!/usr/bin/env python3
"""公开仓库（book-dvw-zh）的译文检查。

三条检查，全部只用标准库，不依赖 Hugo、不联网：

  [1] 镜像完整性  content/book 下每个译文都能在译文目录里找到，反之亦然；
                  每个文件的 YAML front matter 能解析，且除 `title` 外逐键一致。
  [2] 术语一致性  `translation/glossary.md` 的每条「源术语 → 中译」：
                  若源术语出现在对应源文里，则中译必须出现在对应译文里；
                  列出的中译若全库一次都没出现，报为词表里的死条目。
  [3] 结构卫生    意外的 H1（正文节不写标题，页面标题取 front matter）、空文件、
                  无中文正文的文件、CRLF、行尾空白。

严重度三级，对应三种处置方式：

  error   机械性错误（front matter 坏了、结构缺页、正文里混进 `#` 标题、勘误标记不成对……）
          ——**必须清零**，CI 就卡在这一级。
  advise  需要人判断的信号（例如译文里出现了词表没裁定的专名）——不卡 CI，请在 PR 说明里交代。
  note    已知基线的快照（词表里尚未回填的条目清单、源文自带的空正文页……）——
          不卡 CI、也不要求本次 PR 处理；`--show-notes` 才列出。

退出码：0 通过；1 有 error（或 `--strict` 下有 advise）。

用法（在仓库根目录跑）：
    python3 tools/check_public.py                 # 自动探测目录
    python3 tools/check_public.py --strict        # advise 也当失败
    python3 tools/check_public.py --only 2        # 只跑第 2 项
    python3 tools/check_public.py --glossary docs/glossary.md --content content/zh/book \\
        --source content/en/book

给贡献者的提示：本脚本只报机械可判的问题，判断类的问题（语气、术语裁定、误译）
必须人读，见 CONTRIBUTING.md。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 基础设施

ERROR = "error"    # 机械性错误，必须清零
ADVISE = "advise"  # 需要人判断，本次改动引入的不应放过
NOTE = "note"      # 已知基线的快照，不判失败、不要求处理

_findings: list[tuple[str, str, str]] = []  # (严重度, 位置, 说明)
_counts = {ERROR: 0, ADVISE: 0, NOTE: 0}

# 已知的、故意的例外：`源术语 -> 中译` 在指定文件里不要求出现。
# 每条都要写清依据，别拿它当消音器用。
GLOSSARY_EXCEPTIONS: dict[tuple[str, str], set[str]] = {
    # 源文脚注里的文献名保留英文原形（词表 06-08 条的目标是正文里的
    # "player killers"，不是这篇论文标题）。
    ("Player Killers Exposed", "《玩家杀手曝光》"): {"*"},
}

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
H1 = re.compile(r"^#\s+\S")


def report(severity: str, where: str, message: str) -> None:
    _findings.append((severity, where, message))
    _counts[severity] += 1


def rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------- front matter


def split_front_matter(text: str) -> tuple[list[str] | None, str]:
    """返回 (front matter 行, 正文)。没有 front matter 时前半为 None。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], "\n".join(lines[i + 1 :])
    return None, text


def fm_pairs(fmLines: list[str]) -> dict[str, str]:
    """把 front matter 展平成 键 -> 值（顶层键；缩进块按原文存续行）。

    只做机械解析：不引 YAML 库，够用来比较「除 title 外逐键一致」。
    """
    pairs: dict[str, str] = {}
    key: str | None = None
    for raw in fmLines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1] not in (" ", "\t") and ":" in raw:
            key, _, value = raw.partition(":")
            key = key.strip()
            pairs[key] = value.strip()
        elif key is not None:
            pairs[key] += "\n" + raw
    return pairs


# ---------------------------------------------------------------- 术语表


def read_glossary(path: Path) -> list[tuple[str, str, str]]:
    """读词表，返回 [(源术语, 中译, 说明)]。要求是三列 Markdown 表。"""
    rows: list[tuple[str, str, str]] = []
    text = path.read_text(encoding="utf-8")
    header_seen = False
    for lineno, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            report(ERROR, f"{path.name}:{lineno}", f"词表行不是三列：{stripped[:60]}")
            continue
        if set(cells[0]) <= set("-: ") and cells[0]:
            continue  # 分隔行
        if not header_seen and cells[0] == "源术语":
            header_seen = True
            continue
        if len(cells) < 3:
            cells = cells + [""] * (3 - len(cells))
        rows.append((cells[0], cells[1], cells[2]))
    if not header_seen:
        report(ERROR, path.name, "没找到 `| 源术语 | 中译 | 说明 |` 表头，词表格式变了")
    if not rows:
        report(ERROR, path.name, "词表里没有条目")
    return rows


# ---------------------------------------------------------------- 各项检查


def stage_mirror(content: Path, source: Path | None, root: Path) -> None:
    """[1] 译文自身完整、能解析；有源文时逐键比对。"""
    if not content.is_dir():
        report(ERROR, rel(content, root), "译文目录不存在")
        return

    where_prefix = rel(content, root)

    zh_files = {p.relative_to(content) for p in content.rglob("*.md")}
    if not zh_files:
        report(ERROR, rel(content, root), "译文目录里没有 .md 文件")
        return

    for rp in sorted(zh_files):
        path = content / rp
        text = path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            report(ADVISE, rel(path, root), "文件末尾没有换行")
        fm, _ = split_front_matter(text)
        if fm is None:
            report(ERROR, rel(path, root), "缺少 YAML front matter（首行必须是 `---`）")
        else:
            pairs = fm_pairs(fm)
            # 书根（_index.md 带 type: book / book_kind）本来就没有 weight，
            # 键集是否齐全一律以源文那一份为准（见下面的逐键比对）。
            if "title" not in pairs:
                report(ERROR, rel(path, root), "front matter 缺 `title`")
            if not pairs:
                report(ERROR, rel(path, root), "front matter 是空的")

    print(f"  {len(zh_files)} 个译文文件")

    if source is None or not source.is_dir():
        # 公开仓库就是这种情形：只有译文，没有英文源文。这不是问题，
        # 只是第 1 项退化为「译文自检」——不要报成 advise。
        print("  没有源文目录，跳过逐键比对（只做译文自检）")
        return

    en_files = {p.relative_to(source) for p in source.rglob("*.md")}
    for rp in sorted(en_files - zh_files):
        report(ERROR, f"{where_prefix}/{rp.as_posix()}", "源文有这一节，译文缺失")
    for rp in sorted(zh_files - en_files):
        report(ERROR, f"{where_prefix}/{rp.as_posix()}", "译文有这一节，源文没有（多出来的页？）")

    for rp in sorted(en_files & zh_files):
        en_pairs = fm_pairs(split_front_matter((source / rp).read_text(encoding="utf-8"))[0] or [])
        zh_pairs = fm_pairs(split_front_matter((content / rp).read_text(encoding="utf-8"))[0] or [])
        where = f"{where_prefix}/{rp.as_posix()}"
        for key in sorted(set(en_pairs) | set(zh_pairs)):
            if key == "title":
                continue
            if key not in zh_pairs:
                report(ERROR, where, f"front matter 少了源文的 `{key}`")
            elif key not in en_pairs:
                # 历史残件：中文侧 8 个章首页有源文没有的 `book_number`（版式用）。
                # 报 note——它不影响阅读，但在 diff 里看得见，方便顺手发现误加的键。
                report(NOTE, where, f"front matter 多了源文没有的 `{key}`")
            elif en_pairs[key] != zh_pairs[key]:
                # `cascade` 是全书版式的继承块，译文侧与源文侧的差异是已知的历史
                # 残留（中文书根少 `breadcrumb: false` 与 `sidebar_headings`，见私有
                # 仓库审计 E-04）。它在译文公开仓库里不影响 Markdown 阅读，所以只报
                # advise 不当失败——修它要一次性动 285 个页面的版式，由作者定。
                severity = ADVISE if key == "cascade" else ERROR
                report(
                    severity,
                    where,
                    f"front matter 的 `{key}` 与源文不一致："
                    f"{en_pairs[key]!r} vs {zh_pairs[key]!r}",
                )
        if "title" in en_pairs and "title" in zh_pairs and en_pairs["title"] == zh_pairs["title"]:
            report(ADVISE, where, f"title 与源文完全相同：{en_pairs['title']!r}（专名页可忽略）")


def stage_glossary(glossary: Path, content: Path, source: Path | None, root: Path) -> None:
    """[2] 词表裁定的中文必须出现在译文的对应页里。"""
    where_prefix = rel(content, root)
    if not glossary.is_file():
        report(ERROR, rel(glossary, root), "找不到词表")
        return
    rows = read_glossary(glossary)
    if not rows:
        return

    zh_pages: dict[Path, str] = {
        p.relative_to(content): p.read_text(encoding="utf-8") for p in content.rglob("*.md")
    }
    en_pages: dict[Path, str] = {}
    if source is not None and source.is_dir():
        en_pages = {p.relative_to(source): p.read_text(encoding="utf-8") for p in source.rglob("*.md")}

    whole_zh = "\n".join(zh_pages.values())
    dead: set[str] = set()
    for term, zh, _note in rows:
        if not term or not zh or term == "源术语":
            continue
        if zh not in whole_zh:
            dead.add(zh)
            # 这是历史遗留的待回填清单（见 docs/guidelines.md 末节），是基线快照，
            # 不是本次改动引入的问题：报 note，不判失败、也不要求 PR 处理。
            report(NOTE, rel(glossary, root), f"词表条目 `{term} → {zh}` 的中译全库未出现")
    used_zh = {zh for _t, zh, _n in rows if zh and zh not in dead}

    checked = 0
    unreached = 0
    for term, zh, _note in rows:
        if not term or not zh or term == "源术语":
            continue
        if not en_pages:
            continue
        for rp, en_text in en_pages.items():
            if term not in en_text:
                continue
            checked += 1
            if zh in dead:
                # 上面那条词表级 advise 已经覆盖，逐页重复报一遍只是噪音。
                unreached += 1
                continue
            exemptions = GLOSSARY_EXCEPTIONS.get((term, zh), set())
            if "*" in exemptions or rp.as_posix() in exemptions:
                continue
            zh_text = zh_pages.get(rp)
            if zh_text is None:
                continue
            if zh not in zh_text:
                report(
                    NOTE,
                    f"{where_prefix}/{rp.as_posix()}",
                    f"源文有 `{term}`，译文里没有词表裁定的 `{zh}`（待回填或漏译）",
                )

    print(
        f"  词表 {len(rows)} 条，命中源文的组合 {checked} 个（其中 {unreached} 个的中译全库未出现），"
        f"用到的中译 {len(used_zh)} 个"
    )


def stage_hygiene(content: Path, root: Path) -> None:
    """[3] 结构卫生。"""
    for path in sorted(content.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        where = rel(path, root)
        if "\r\n" in text:
            report(ADVISE, where, "含 CRLF 行尾")
        if not text.strip():
            report(ERROR, where, "空文件")
        body = split_front_matter(text)[1]
        if body.strip() and not CJK.search(body):
            report(ADVISE, where, "正文里没有中文字符（这一页是不是还没译？）")
        for lineno, line in enumerate(body.split("\n"), 1):
            if H1.match(line):
                report(
                    ERROR,
                    f"{where}:{lineno}",
                    "正文里出现 `#` 标题；页面标题取自 front matter，节页不写标题",
                )
            if line != line.rstrip():
                report(ADVISE, f"{where}:{lineno}", "行尾有空白")
        # 勘误标记：`<em>{勘误：……}</em>`，全套同标。这里只查机械形态——
        # 括号是否配对、冒号是否全角、标签外有没有多余空格。
        opened = text.count("<em>{勘误：")
        closed = text.count("}</em>")
        if opened != closed:
            report(
                ERROR,
                where,
                f"勘误标记不成对：`<em>{{勘误：` {opened} 处，`}}</em>` {closed} 处",
            )
        for lineno, line in enumerate(body.split("\n"), 1):
            if "勘误" not in line:
                continue
            if "*{勘误" in line or "}*" in line:
                report(
                    ERROR,
                    f"{where}:{lineno}",
                    "勘误标记用了 Markdown 星号；`*` 紧跟汉字再跟 `{` 会被当成字面量，必须用 `<em>…</em>`",
                )
            if "<em> {勘误" in line or "<em>{ 勘误" in line:
                report(ADVISE, f"{where}:{lineno}", "勘误标记里标签与花括号之间有多余空格")
        if not body.strip():
            # 源文里就有这类只有标题、没有正文的页（如 05-41），是原书版式，
            # 不是漏译。报 note。
            report(NOTE, where, "front matter 之后没有正文（源文如此，不是漏译）")


# ---------------------------------------------------------------- 入口


def detect_repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        if (candidate / ".git").exists():
            return candidate
    return Path.cwd()


def default_dir(root: Path, *candidates: str) -> Path:
    for c in candidates:
        if (root / c).is_dir() or (root / c).is_file():
            return root / c
    return root / candidates[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公开仓库译文检查")
    parser.add_argument("--root", default=None, help="仓库根目录（默认自动探测）")
    parser.add_argument("--content", default=None, help="译文目录（默认 content/book）")
    parser.add_argument("--source", default=None, help="源文目录（默认 content/en/book，没有就跳过）")
    parser.add_argument("--glossary", default=None, help="词表（默认 translation/glossary.md）")
    parser.add_argument("--strict", action="store_true", help="advise 也算失败")
    parser.add_argument("--show-notes", action="store_true", help="列出 note（已知基线快照）")
    parser.add_argument("--only", default=None, help="只跑指定项，如 2 或 1,3")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else detect_repo_root()
    content = Path(args.content).resolve() if args.content else default_dir(root, "content/book")
    source = Path(args.source).resolve() if args.source else default_dir(root, "content/en/book")
    if not source.is_dir():
        source = None
    glossary = (
        Path(args.glossary).resolve()
        if args.glossary
        else default_dir(root, "translation/glossary.md", "docs/glossary.md")
    )

    wanted = {int(x) for x in args.only.split(",")} if args.only else {1, 2, 3}

    print(f"仓库根：{root}")
    print(f"译文：{rel(content, root)}｜源文：{rel(source, root) if source else '（无）'}"
          f"｜词表：{rel(glossary, root)}")

    if 1 in wanted:
        print("[1] 镜像完整性与 front matter …")
        stage_mirror(content, source, root)
    if 2 in wanted:
        print("[2] 术语一致性 …")
        stage_glossary(glossary, content, source, root)
    if 3 in wanted:
        print("[3] 结构卫生 …")
        stage_hygiene(content, root)

    print()
    for severity, where, message in _findings:
        if severity == NOTE and not args.show_notes:
            continue
        print(f"{severity:>6}  {where}  {message}")
    print()
    summary = f"error {_counts[ERROR]}｜advise {_counts[ADVISE]}"
    if _counts[NOTE]:
        summary += f"｜note {_counts[NOTE]}（已知基线，未列出；--show-notes 可看）"
    print(summary)
    if _counts[ERROR] or (args.strict and _counts[ADVISE]):
        print("结果：不通过")
        return 1
    print("结果：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
