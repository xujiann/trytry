#!/usr/bin/env python3
"""书稿（book/*.md）→ 印刷级样书 PDF。

按图书正式出版要求排版：
  * 开本 787mm×1092mm 1/16（成品 185mm×260mm），镜像页边距（订口宽、切口窄）；
  * 页面顺序：封面 → 封二 → 扉页 → 版权页 → 前言 → 目录 → 五篇十九章 → 附录 → 后记 → 封三 → 封底；
  * 前置部分排小写罗马页码，正文自第一篇篇章页起排阿拉伯页码；
  * 篇章页单页起（右手页），章另页起，章首页不排书眉；
  * 双码页书眉排书名、单码页书眉排章题，页码排版口外侧；
  * 目录自动生成（篇 / 章 / 节 / 小节四级，带页码与引导点），代替手工维护的 01_目录.md；
  * 总页数自动补白为偶数，保证双面印刷时封底落在纸张背面。

正文中文行合并：Markdown 源文件为硬换行，直接转换会在中文句子中间引入
多余空格（旧版书稿 PDF 的通病）。build 前先做 CJK 感知的段内合行：
两侧均为中文时直接相连，任一侧为西文时保留一个空格（与作者行内风格一致）。

去掉的与出版无关内容：
  * 00_书名页.md 末尾的成稿过程注记（"成稿于平台第十二轮……"）；
  * 01_目录.md（手工目录，由自动目录取代）；
  其余前置内容重新归位：内容简介 / 读者对象 → 版权页"内容提要"，
  数据说明 → 版权页"数据与隐私说明"，四问与三条立场 → 封面 / 封底文案。

依赖（仅构建期，非平台运行时依赖）：weasyprint、markdown、pymupdf（可选，
仅用于导出封面 PNG）。用法见同目录 README.md。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import markdown as md_lib
from weasyprint import HTML

BUILD_DIR = Path(__file__).resolve().parent
BOOK_DIR = BUILD_DIR.parent

# ---------------------------------------------------------------- 基本信息
TITLE = "紧密型县域医共体信息化建设"
SUBTITLE_DASH = "从功能指引到工程落地"
SUBTITLE_REST = "一个完整参考实现的设计、实现与工程方法"
TITLE_EN = "COUNTY MEDICAL COMMUNITY INFORMATIZATION · AN ENGINEERING RECORD"
AUTHOR = "徐剑"
# 占位：正式付印前替换为实际出版社名（封面、封底、扉页、版权页同步生效）。
PUBLISHER = "××出版社"

OUT_PDF = BOOK_DIR / f"{TITLE}_样书.pdf"
OUT_COVER_PNG = BOOK_DIR / "封面_300dpi.png"

# ---------------------------------------------------------------- 篇章结构
PREFACE_FILE = "02_前言.md"
POSTSCRIPT_FILE = "99_后记.md"

PARTS: list[tuple[str, str, list[str]]] = [
    ("第一篇", "总论与顶层设计", [
        "10_第1章_紧密型县域医共体与信息化的使命.md",
        "11_第2章_总体架构.md",
        "12_第3章_数据模型的顶层决策.md",
    ]),
    ("第二篇", "五大类协同应用的实现", [
        "13_第4章_区域医疗服务协同.md",
        "14_第5章_便民惠民服务协同.md",
        "15_第6章_医疗管理服务协同.md",
        "16_第7章_公共卫生服务协同.md",
        "17_第8章_基层医疗卫生综合管理.md",
    ]),
    ("第三篇", "工程正确性：十二个实测确认的缺陷", [
        "18_第9章_并发写入缺陷家族.md",
        "19_第10章_读改写与丢更新.md",
        "20_第11章_金额精度.md",
        "21_第12章_事务与事务边界.md",
    ]),
    ("第四篇", "数据安全与隐私", [
        "22_第13章_纵向越权与横向越权.md",
        "23_第14章_横向数据隔离模型.md",
        "24_第15章_敏感操作留痕.md",
        "25_第16章_等保密码应用与信创的边界.md",
    ]),
    ("第五篇", "工程方法论", [
        "26_第17章_让测试真正防住回归.md",
        "27_第18章_代码审阅的实证方法.md",
        "28_第19章_演进的节奏.md",
    ]),
    ("", "附录", [
        "80_附录A_功能指引对照表.md",
        "81_附录B_数据字典.md",
        "82_附录C_接口清单.md",
        "83_附录D_部署与运维指引.md",
        "84_附录E_缺陷索引.md",
        "85_附录F_术语表.md",
    ]),
]

# ---------------------------------------------------------------- 中文合行
_EXTRA_CJK = "–—‘’“”…·"


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x2E80 <= o <= 0x9FFF
        or 0x3000 <= o <= 0x303F
        or 0xFF00 <= o <= 0xFFEF
        or ch in _EXTRA_CJK
    )


def _join_sep(prev: str, nxt: str) -> str:
    """合并两行时的分隔符：两侧皆为中文（含中文标点）则不加空格，否则加一个。"""
    t = prev.rstrip()
    while t and t[-1] in "*`":
        t = t[:-1]
    h = nxt.lstrip()
    while h and h[0] in "*`":
        h = h[1:]
    if t and h and _is_cjk(t[-1]) and _is_cjk(h[0]):
        return ""
    return " "


_BLOCK_STARTER = re.compile(r"(#{1,6}\s|```|\||[-*+]\s|\d+[.)]\s|---+\s*$)")
_NO_CONTINUE_PREV = re.compile(r"(#{1,6}\s|\||---+\s*$)")


def unwrap_cjk(text: str) -> str:
    """把段内硬换行合并成整段，避免 Markdown 在中文之间插入空格。"""
    out: list[str] = []
    fence = False
    for raw in text.split("\n"):
        if raw.lstrip().startswith("```"):
            fence = not fence
            out.append(raw)
            continue
        if fence:
            out.append(raw)
            continue
        cur = raw.strip()
        prev = out[-1].strip() if out else ""
        mergeable = bool(
            prev
            and cur
            and not _NO_CONTINUE_PREV.match(prev)
            and not _BLOCK_STARTER.match(cur)
        )
        if mergeable and prev.startswith(">") != cur.startswith(">"):
            mergeable = False
        if mergeable:
            if cur.startswith(">"):
                cur = cur[1:].strip()
            out[-1] = out[-1].rstrip() + _join_sep(prev, cur) + cur
        else:
            out.append(raw)
    return "\n".join(out)


# ---------------------------------------------------------------- 引号规范化
QUOTE_WARNINGS: list[str] = []


def _curly_line(line: str, where: str) -> str:
    """把一行里的直引号成对转为全角弯引号（"…" / '…'）。

    行内代码段（反引号包裹）不动；本行内直引号数量为奇数时整行不转并登记
    告警——宁可保留直引号，也不产出配错方向的弯引号。合行（unwrap_cjk）
    在先，因此跨行的引号对此时已并回同一行。
    """
    parts = line.split("`")
    texts = [p for i, p in enumerate(parts) if i % 2 == 0]
    n_d = sum(p.count('"') for p in texts)
    n_s = sum(p.count("'") for p in texts)
    do_d, do_s = n_d > 0 and n_d % 2 == 0, n_s > 0 and n_s % 2 == 0
    if n_d % 2 or n_s % 2:
        QUOTE_WARNINGS.append(f"{where}: 引号不成对，保留直引号：{line.strip()[:50]}")
    if not (do_d or do_s):
        return line
    open_d = open_s = False
    for i, seg in enumerate(parts):
        if i % 2 == 1:  # 行内代码
            continue
        out = []
        for ch in seg:
            if ch == '"' and do_d:
                out.append("”" if open_d else "“")
                open_d = not open_d
            elif ch == "'" and do_s:
                out.append("’" if open_s else "‘")
                open_s = not open_s
            else:
                out.append(ch)
        parts[i] = "".join(out)
    return "`".join(parts)


def curly_quotes(text: str, where: str) -> str:
    out = []
    fence = False
    for i, ln in enumerate(text.split("\n"), 1):
        if ln.lstrip().startswith("```"):
            fence = not fence
            out.append(ln)
            continue
        out.append(ln if fence else _curly_line(ln, f"{where}:{i}"))
    return "\n".join(out)


# ---------------------------------------------------------------- Markdown
def scan_headings(text: str) -> list[tuple[int, str]]:
    """围栏感知地提取 #/##/### 标题（围栏内的 # 是代码注释，跳过）。"""
    res: list[tuple[int, str]] = []
    fence = False
    for ln in text.split("\n"):
        if ln.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        m = re.match(r"(#{1,3})\s+(.*?)\s*$", ln)
        if m:
            res.append((len(m.group(1)), m.group(2)))
    return res


def md_to_html(text: str) -> str:
    return md_lib.markdown(text, extensions=["tables", "fenced_code"])


def add_heading_ids(html: str, ids: list[str | None]) -> str:
    it = iter(ids)

    def repl(m: re.Match) -> str:
        hid = next(it, None)
        if hid:
            return f'<{m.group(1)} id="{hid}">'
        return m.group(0)

    return re.sub(r"<(h[23])>", repl, html)


def plain_heading(text: str) -> str:
    """目录条目文字：去掉行内 Markdown 记号。"""
    return text.replace("**", "").replace("`", "")


# ---------------------------------------------------------------- 章构建
CH_H1 = re.compile(r"^第\s*(\d+)\s*章[　\s]+(.*)$")
AP_H1 = re.compile(r"^附录\s*([A-Z])[　\s]+(.*)$")


class Chapter:
    def __init__(self, path: Path, cid: str | None = None):
        self.path = path
        raw = curly_quotes(unwrap_cjk(path.read_text("utf-8")), path.name)
        heads = scan_headings(raw)
        assert heads and heads[0][0] == 1, f"{path.name} 缺少一级标题"
        h1 = heads[0][1]
        m = CH_H1.match(h1)
        a = AP_H1.match(h1)
        if m:
            self.cid, self.label, self.title = f"ch{m.group(1)}", f"第 {m.group(1)} 章", m.group(2)
            self.kind = "chapter"
        elif a:
            self.cid, self.label, self.title = f"app{a.group(1)}", f"附录 {a.group(1)}", a.group(2)
            self.kind = "appendix"
        else:
            self.cid, self.label, self.title = path.stem, "", h1
            self.kind = "plain"
        if cid:
            self.cid = cid
        # 去掉源文件里的一级标题行，由脚本生成章首排式
        body_md = re.sub(r"^#\s+.*?\n", "", raw, count=1)
        self.sub_heads = [(lv, t) for lv, t in heads[1:]]
        ids: list[str | None] = []
        self.toc_children: list[tuple[int, str, str]] = []  # (级别, 文字, 锚点)
        for i, (lv, t) in enumerate(self.sub_heads):
            hid = f"{self.cid}-h{i + 1}"
            ids.append(hid)
            # 目录收录规则：节（##）一律收；小节（###）只收正文章（附录 B/C 的
            # ### 是数据表名 / 接口前缀，收进目录会淹没结构）。
            if lv == 2 or (lv == 3 and self.kind == "chapter"):
                self.toc_children.append((lv, plain_heading(t), hid))
        self.html_body = add_heading_ids(md_to_html(body_md), ids)

    @property
    def toc_text(self) -> str:
        return f"{self.label}　{self.title}" if self.label else self.title

    def html(self, extra_class: str = "") -> str:
        no_html = f'<span class="chap-no">{self.label}　</span>' if self.label else ""
        klass = f"chapter {self.kind} {extra_class}".strip()
        return (
            f'<section class="{klass}" id="{self.cid}">\n'
            f'<header class="chap-open">'
            f'<h1 class="chap-title">{no_html}{self.title}</h1>'
            f"</header>\n{self.html_body}\n</section>\n"
        )


# ---------------------------------------------------------------- 前置内容
def load_front_texts() -> dict[str, str]:
    """从 00_书名页.md 提取版权页与封底所需文字（保持与源稿同步）。"""
    raw = curly_quotes(unwrap_cjk((BOOK_DIR / "00_书名页.md").read_text("utf-8")), "00_书名页.md")

    def grab(pattern: str) -> str:
        m = re.search(pattern, raw, re.S)
        return m.group(1).strip() if m else ""

    readers = grab(r"\*\*读者对象\*\*：(.*?)(?:\n\n|\n---)").rstrip("。")
    data_note = grab(r"\*\*数据说明\*\*：(.*?)(?:\n\n|\n---)")
    # 提要首段已交代"基于真实参考实现撰写"，此处去重，直入数据口径
    data_note = re.sub(r"^本书基于.*?撰写，全部", "本书全部", data_note)
    return {"readers": readers, "data_note": data_note}


def front_matter_html(imprint: dict[str, str]) -> str:
    ft = load_front_texts()
    abstract = (
        "本书基于一个真实、完整、可运行的紧密型县域医共体信息化平台参考实现撰写，"
        "覆盖国家《紧密型县域医共体信息化功能指引》五大类 36 项功能，含 187 张数据表、"
        "647 个接口、约 3.4 万行服务端代码、1200 余个自动化测试用例。全书分五篇：总论与"
        "顶层设计厘清指引到架构的映射；协同应用实现逐类展开三十六项功能的数据模型与业务"
        "口径；工程正确性收录十二个实测确认并修复的缺陷，归纳出可复用的正确写法；数据安全"
        "与隐私系统讲述横向数据隔离、敏感操作留痕与等保密码合规的边界；工程方法论提炼"
        "“如何让测试真正防住回归”的一套做法。附录给出功能对照表、数据字典、接口清单与"
        "部署运维指引。它既是一份“建什么”的参考实现说明，更是一部“怎样把它建对”的工程实录。"
    )
    return f"""
<section class="cover" id="cover-front">
  <div class="cv-ground"></div>
  <div class="cv-band"></div>
  <div class="cv-band-accent"></div>
  <div class="cv-topline"></div>
  <div class="cv-series">{TITLE_EN}</div>
  <div class="cv-title">紧密型县域医共体<br>信息化建设</div>
  <div class="cv-subtitle-rule"></div>
  <div class="cv-subtitle">{SUBTITLE_DASH}<br>{SUBTITLE_REST}</div>
  <div class="cv-author">{AUTHOR}　著</div>
  <div class="cv-motif">{motif_svg()}</div>
  <div class="cv-points">
    <b>并发下账对不对</b>——最后一支疫苗，四人同时登记，会不会四针都“成功”？<br>
    <b>跨机构能不能看</b>——数据汇到一起，不等于谁都能看所有人。<br>
    <b>合规查不查得出</b>——“谁查了谁的档案”，必须答得出来。<br>
    <b>测试绿了就对了吗</b>——绿色的测试，不等于正确的代码。
  </div>
  <div class="cv-press">{PUBLISHER}</div>
</section>

<section class="blankpage"><div class="blankfill"></div></section>

<section class="titlepage">
  <div class="tp-space"></div>
  <div class="tp-title">紧密型县域医共体<br>信息化建设</div>
  <div class="tp-subtitle">——{SUBTITLE_DASH}：{SUBTITLE_REST}</div>
  <div class="tp-author">{AUTHOR}　著</div>
  <div class="tp-press">{PUBLISHER}</div>
</section>

<section class="copyright">
  <div class="cp-abstract">
    <h2>内容提要</h2>
    <p>{abstract}</p>
    <p>本书适合{ft["readers"]}阅读。</p>
  </div>
  <div class="cp-block">
    <p><strong>数据与隐私说明</strong>　{ft["data_note"]}</p>
  </div>
  <hr class="cp-rule">
  <div class="cp-imprint">
    <p><strong>书　　名</strong>　{TITLE}——{SUBTITLE_DASH}：{SUBTITLE_REST}</p>
    <p><strong>著　　者</strong>　{AUTHOR}</p>
    <p><strong>出版发行</strong>　{PUBLISHER}</p>
    <p><strong>开　　本</strong>　787mm × 1092mm　1/16</p>
    <p><strong>印　　张</strong>　{imprint["sheets"]}</p>
    <p><strong>字　　数</strong>　约 {imprint["kchars"]} 千字</p>
    <p><strong>版次 / 印次</strong>　待定</p>
    <p><strong>ISBN</strong>　待申请</p>
    <p><strong>定　　价</strong>　待定</p>
  </div>
  <p class="cp-note">本册为排版审校样书。出版社名称、CIP 数据、ISBN、版次与定价均为占位，
  付印前由出版社确定后替换；图书在版编目（CIP）数据待申请后补入本页。</p>
</section>
"""


def motif_svg() -> str:
    """封面装饰：县—乡—村三级网络示意（矢量，随封面同印）。"""
    return """<svg width="230" height="252" viewBox="0 0 230 252" xmlns="http://www.w3.org/2000/svg">
<g stroke="#7FA8BC" stroke-width="1" opacity="0.55" fill="none">
  <line x1="115" y1="96" x2="38"  y2="30"/>
  <line x1="115" y1="96" x2="196" y2="40"/>
  <line x1="115" y1="96" x2="30"  y2="170"/>
  <line x1="115" y1="96" x2="192" y2="180"/>
  <line x1="115" y1="96" x2="118" y2="222"/>
  <line x1="38"  y1="30"  x2="12"  y2="64"/>
  <line x1="38"  y1="30"  x2="70"  y2="12"/>
  <line x1="196" y1="40"  x2="222" y2="70"/>
  <line x1="196" y1="40"  x2="164" y2="14"/>
  <line x1="30"  y1="170" x2="10"  y2="204"/>
  <line x1="30"  y1="170" x2="62"  y2="200"/>
  <line x1="192" y1="180" x2="220" y2="206"/>
  <line x1="118" y1="222" x2="88"  y2="244"/>
  <line x1="118" y1="222" x2="150" y2="244"/>
</g>
<g fill="none">
  <circle cx="115" cy="96" r="34" stroke="#C8A45C" stroke-width="1.6" opacity="0.95"/>
  <circle cx="115" cy="96" r="44" stroke="#7FA8BC" stroke-width="0.8" opacity="0.4" stroke-dasharray="3 4"/>
  <path d="M115 82 v28 M101 96 h28" stroke="#C8A45C" stroke-width="3.4" opacity="0.95"/>
</g>
<g fill="#9FBCCB" opacity="0.85">
  <circle cx="38"  cy="30"  r="7"/>
  <circle cx="196" cy="40"  r="7"/>
  <circle cx="30"  cy="170" r="7"/>
  <circle cx="192" cy="180" r="7"/>
  <circle cx="118" cy="222" r="7"/>
</g>
<g fill="#7FA8BC" opacity="0.6">
  <circle cx="12"  cy="64"  r="3"/><circle cx="70"  cy="12"  r="3"/>
  <circle cx="222" cy="70"  r="3"/><circle cx="164" cy="14"  r="3"/>
  <circle cx="10"  cy="204" r="3"/><circle cx="62"  cy="200" r="3"/>
  <circle cx="220" cy="206" r="3"/><circle cx="88"  cy="244" r="3"/>
  <circle cx="150" cy="244" r="3"/>
</g>
</svg>"""


def back_cover_html() -> str:
    return f"""
<section class="cover" id="cover-back">
  <div class="bk-ground"></div>
  <div class="bk-topline"></div>
  <div class="bk-lead">市面上不缺“该建什么”的书，<br>缺的是“怎么建对”。</div>
  <div class="bk-questions">
    <b>并发下账对不对</b>——四个人同时给最后一支库存的疫苗登记接种，会不会四针全部“成功”？
    一笔结余在三家机构间均分，分完了会不会凭空少一分钱？<br>
    <b>跨机构能不能看</b>——医共体把全县的数据汇到一起，可“汇到一起”不等于“谁都能看所有人”。<br>
    <b>合规查不查得出</b>——《个人信息保护法》要求“谁查了谁的档案必须答得出来”，
    一张只记不能查的日志，等于没记。<br>
    <b>测试绿了就对了吗</b>——一条只覆盖 11% 情形的检查规则会全程亮绿灯，
    一个带着“1 元容差”的断言能把丢钱的 bug 藏六轮。
  </div>
  <div class="bk-stance">
    <span class="st">实证优先，不写空头支票</span>——每一处“已实现”都对应可运行的代码与可复现的测试。<br>
    <span class="st">缺陷比功能更值得写</span>——别人的教训是最便宜的学费。<br>
    <span class="st">警惕“看起来对了”</span>——绿色的测试不等于正确的代码。
  </div>
  <div class="bk-close">它既是一份“建什么”的参考实现说明，<br>更是一部“怎样把它建对”的工程实录。</div>
  <div class="bk-isbnbox">ISBN　待申请<br>定价　待定</div>
  <div class="bk-press">{PUBLISHER}</div>
</section>
"""


# ---------------------------------------------------------------- 目录
def toc_html(parts: list[tuple[str, str, list[Chapter]]]) -> str:
    rows: list[str] = ['<section class="toc" id="toc">',
                       '<h1 class="toc-title">目录</h1>', '<nav class="toc">']
    rows.append('<p class="toc-front"><a class="roman" href="#preface">前言</a></p>')
    for pi, (plabel, ptitle, chapters) in enumerate(parts, 1):
        ptext = f"{plabel}　{ptitle}" if plabel else ptitle
        rows.append(f'<p class="toc-part"><a href="#part-{pi}">{ptext}</a></p>')
        for ch in chapters:
            rows.append(f'<p class="toc-ch"><a href="#{ch.cid}">{ch.toc_text}</a></p>')
            for lv, text, hid in ch.toc_children:
                rows.append(f'<p class="toc-s{lv}"><a href="#{hid}">{text}</a></p>')
    rows.append('<p class="toc-back"><a href="#postscript">后记</a></p>')
    rows.append("</nav></section>")
    return "\n".join(rows)


def part_html(index: int, label: str, title: str, chapters: list[Chapter]) -> str:
    first = " first" if index == 1 else ""
    lis = "\n".join(f"<li>{c.toc_text}</li>" for c in chapters)
    return (
        f'<section class="part{first}" id="part-{index}">\n'
        f'<div class="part-inner">\n'
        f'<div class="part-label">{label}</div>\n'
        f'<h1 class="part-title">{title}</h1>\n'
        f'<div class="part-sep"></div>\n'
        f'<ul class="part-chapters">\n{lis}\n</ul>\n'
        f"</div>\n</section>\n"
    )


# ---------------------------------------------------------------- 组装
def count_kchars() -> int:
    total = 0
    files = [PREFACE_FILE, POSTSCRIPT_FILE] + [f for _, _, fs in PARTS for f in fs]
    for f in files:
        text = (BOOK_DIR / f).read_text("utf-8")
        total += len(re.sub(r"\s", "", text))
    return round(total / 1000)


def build_html(imprint: dict[str, str], extra_blank: bool) -> str:
    preface = Chapter(BOOK_DIR / PREFACE_FILE, cid="preface")
    postscript = Chapter(BOOK_DIR / POSTSCRIPT_FILE, cid="postscript")
    # 后记签名行右对齐
    postscript.html_body = postscript.html_body.replace("<p>——徐剑", '<p class="signature">——徐剑')

    parts: list[tuple[str, str, list[Chapter]]] = []
    for plabel, ptitle, files in PARTS:
        parts.append((plabel, ptitle, [Chapter(BOOK_DIR / f) for f in files]))

    css = (BUILD_DIR / "styles.css").read_text("utf-8")
    pieces: list[str] = [
        "<html><head><meta charset='utf-8'>",
        f"<title>{TITLE}</title>",
        f"<meta name='author' content='{AUTHOR}'>",
        f"<meta name='description' content='{SUBTITLE_DASH}：{SUBTITLE_REST}'>",
        f"<style>{css}</style></head><body>",
        front_matter_html(imprint),
        # 前言（前置部分，罗马页码从 i 起）
        preface.html(extra_class="preface frontmatter"),
        toc_html(parts),
    ]
    for pi, (plabel, ptitle, chapters) in enumerate(parts, 1):
        pieces.append(part_html(pi, plabel, ptitle, chapters))
        for ch in chapters:
            pieces.append(ch.html())
    pieces.append(postscript.html(extra_class="backmatter recto"))
    if extra_blank:  # 补白页：保证总页数为偶数、封底落在纸张背面
        pieces.append('<section class="blankpage"><div class="blankfill"></div></section>')
    pieces.append('<section class="blankpage"><div class="blankfill"></div></section>')  # 封三
    pieces.append(back_cover_html())
    pieces.append("</body></html>")
    return "\n".join(pieces)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=OUT_PDF)
    ap.add_argument("--no-cover-png", action="store_true", help="不导出封面 PNG")
    args = ap.parse_args()

    kchars = count_kchars()
    imprint = {"sheets": "?", "kchars": str(kchars)}

    # 第一遍：得到总页数（用于印张与偶数页补白）
    print("· 第一遍渲染（统计页数）……", flush=True)
    doc = HTML(string=build_html(imprint, extra_blank=False), base_url=str(BUILD_DIR)).render()
    if QUOTE_WARNINGS:
        print(f"⚠ {len(QUOTE_WARNINGS)} 处引号不成对（保留直引号）：")
        for w in QUOTE_WARNINGS:
            print("   ", w)
        QUOTE_WARNINGS.clear()  # 第二遍会重复登记，无需再报
    n = len(doc.pages)
    extra_blank = n % 2 == 1
    total = n + (1 if extra_blank else 0)
    imprint["sheets"] = f"{total / 16:.2f}".rstrip("0").rstrip(".")

    print(f"· 第一遍 {n} 页 → 成品 {total} 页（{'补 1 页空白' if extra_blank else '无需补白'}），"
          f"印张 {imprint['sheets']}，约 {kchars} 千字", flush=True)

    print("· 第二遍渲染（最终版）……", flush=True)
    doc = HTML(string=build_html(imprint, extra_blank=extra_blank), base_url=str(BUILD_DIR)).render()
    assert len(doc.pages) == total, f"两遍页数不一致：{len(doc.pages)} != {total}"
    doc.write_pdf(args.out)
    print(f"✓ 已生成 {args.out}（{total} 页）", flush=True)

    if not args.no_cover_png:
        try:
            import pymupdf
        except ImportError:
            print("· 未安装 pymupdf，跳过封面 PNG 导出（pip install pymupdf）")
            return
        pdf = pymupdf.open(str(args.out))
        pix = pdf[0].get_pixmap(dpi=300)
        pix.save(str(OUT_COVER_PNG))
        print(f"✓ 已导出封面 {OUT_COVER_PNG}（{pix.width}×{pix.height}px @300dpi）")


if __name__ == "__main__":
    sys.exit(main())
