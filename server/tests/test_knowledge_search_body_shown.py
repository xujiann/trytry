"""知识检索表每行能「查看正文」，正文一律转义、多行照录入的换行显示（P2-1667，第四十九批扫描 AM4-2）。

检索接口（`GET /api/knowledge`，出参 `EntrySearchOut`）一直把正文 `body` 返回给页面；修前知识库页的检索表只列 ID / 分类 /
标题 / 有效期至 / 状态 / 操作六列，全仓前端没有一处引用 `k.body`——医生搜得到「国家基本药物目录…使用管理办法」，读不到
一个字（扫描实测：检索出参 `body` 29 个字，页面上哪儿都看不见）。

修后（接口不改）：检索表抽成顶层的 `renderKbTable`，每行操作列带「查看正文」，紧跟一条默认收着的展开行，正文 `esc()`、
`white-space:pre-wrap`，没填的写 —（照 P2-1408 医案行的写法）；点「查看正文」切换这一条的展开行，按钮字跟着换。
不能编辑的角色原先操作列是「—」，现在也有「查看正文」。修订正文的入口不在这一条里。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PUBLIC = (STATIC / "pages-public.js").read_text(encoding="utf-8")
SHARED = (STATIC / "shared.js").read_text(encoding="utf-8")
CORE = (STATIC / "core.js").read_text(encoding="utf-8")
#: 正文里夹一段脚本：展开后必须原样转义显示，不能进 DOM
PAYLOAD = "<script>alert(1)</script>"
#: node 里没有 DOM：`$` 走 document.querySelector，这里用不到元素
PRELUDE = "globalThis.document = { addEventListener() {}, querySelector() { return null; }, cookie: '' };\n"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


def _top_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    return source[start:source.index("\n}\n", start) + 2]


def _knowledge() -> str:
    start = PUBLIC.index("async function renderKnowledge()")
    return PUBLIC[start:PUBLIC.index("\n}\n", start) + 2]


def _node(script: str, *args: str) -> str:
    done = subprocess.run(["node", "-e", PRELUDE + SHARED + "\n" + script, *args],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _render(rows: list[dict], can_edit: bool) -> str:
    script = (_top_function(CORE, "table") + _top_function(PUBLIC, "renderKbTable")
              + "\nprocess.stdout.write(renderKbTable(JSON.parse(process.argv[1]), process.argv[2] === '1'));\n")
    return _node(script, json.dumps(rows, ensure_ascii=False), "1" if can_edit else "0")


@pytest.fixture(scope="module")
def entries(client, admin):
    """两条：一条正文多行、夹一段脚本；一条只填了分类与标题（正文空着）。"""
    out = {}
    for key, payload in (
        ("policy", {"category": "drug_policy", "title": "P21667 国家基本药物目录使用管理办法",
                    "body": f"第一条 基层医疗机构基本药物配备使用比例不低于 90%\n第二条 {PAYLOAD}"}),
        ("blank", {"category": "clinical_guideline", "title": "P21667 高血压基层诊疗指南"}),
    ):
        resp = client.post("/api/knowledge", headers=admin, json=payload)
        assert resp.status_code == 201, resp.text
        out[key] = resp.json()["id"]
    return out


@needs_node
@pytest.mark.parametrize("can_edit", [True, False])
def test_检索行带查看正文_展开行默认收着_正文转义保留换行(client, admin, entries, can_edit):
    rows = client.get("/api/knowledge", headers=admin, params={"q": "P21667"}).json()
    by_id = {k["id"]: k for k in rows}
    assert by_id[entries["policy"]]["body"].endswith(PAYLOAD)   # 出参一直带着正文，只是页面不画
    html = _render(rows, can_edit)
    for kid in entries.values():
        assert f'<button class="btn secondary" data-kbopen="{kid}">查看正文</button>' in html   # 修前没有
        assert f'<tr class="hidden" data-kbbody="{kid}"><td colspan="6">' in html               # 默认收着
    body = re.search(rf'data-kbbody="{entries["policy"]}"><td colspan="6">(.*?)</td></tr>', html, re.S).group(1)
    assert 'style="white-space:pre-wrap' in body
    assert ("第一条 基层医疗机构基本药物配备使用比例不低于 90%\n第二条 &lt;script&gt;alert(1)&lt;/script&gt;"
            in body)                                                               # 多行原样、脚本转义
    assert PAYLOAD not in html                                                     # 正文里的标签不进 DOM
    blank = re.search(rf'data-kbbody="{entries["blank"]}"><td colspan="6">(.*?)</td></tr>', html, re.S).group(1)
    assert blank.endswith(">—</div>")                                              # 没填的写 —
    # 续期 / 停用照旧只给能编辑的角色
    assert (f'data-renew="{entries["policy"]}"' in html) is can_edit
    assert (f'data-deact="{entries["policy"]}"' in html) is can_edit


@needs_node
def test_点查看正文展开这一条_再点收起():
    """展开行紧跟在条目行后面：点「查看正文」切换它的 hidden，按钮字跟着换；不是这一条的展开行不动，也不发请求。"""
    body = _knowledge()
    start = body.index('$("#page-body").onclick = async (e) => {')
    handler = body[start:body.index("\n  };", start) + len("\n  };")]
    script = (
        "const classes = new Set(['hidden']);\n"
        "const detail = { dataset: { kbbody: '7' }, classList: {"
        " toggle(c) { classes.has(c) ? classes.delete(c) : classes.add(c); }, contains(c) { return classes.has(c); } } };\n"
        "const row = { nextElementSibling: detail };\n"
        "async function api() { throw new Error('查看正文不该发请求'); }\n"
        "async function spdModal() { throw new Error('查看正文不该弹窗'); }\n"
        "function setMsg(sel, msg) { throw new Error(msg); }\n"
        "const handlers = {};\n"
        + handler.replace('$("#page-body").onclick', "handlers.click")
        + "\n(async () => {\n"
        "  const button = { dataset: { kbopen: '7' }, textContent: '查看正文', closest: (sel) => (sel === 'tr' ? row : null) };\n"
        "  const seen = [];\n"
        "  for (let i = 0; i < 2; i++) { await handlers.click({ target: button }); seen.push([classes.has('hidden'), button.textContent]); }\n"
        "  const stranger = { dataset: { kbopen: '8' }, textContent: '查看正文', closest: () => row };\n"
        "  await handlers.click({ target: stranger });\n"
        "  seen.push([classes.has('hidden'), stranger.textContent]);\n"
        "  process.stdout.write(JSON.stringify(seen));\n})();\n"
    )
    assert json.loads(_node(script)) == [[False, "收起正文"], [True, "查看正文"], [True, "查看正文"]]


def test_检索结果交给renderKbTable画():
    body = _knowledge()
    assert '$("#kb-table").innerHTML = renderKbTable(entries, canEdit);' in body
