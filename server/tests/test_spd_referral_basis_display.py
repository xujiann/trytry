"""慢专病转诊单的触发依据与转诊资料三端都不显示（P2-1609，第四十七批扫描 AK4-5 显示那一半）。

接口出参早就带着 `trigger_evidence`（规则开单时存 `{"matched": [命中的条件…]}`）与 `materials`：`spd/routers/referral.py`
的清单 / 详情，居民端 `spd/routers/portal.py` 的转诊详情。三端原先都不画——管理端「全轨迹」只画轨迹，医生移动端的转诊
（审核）卡片只有理由，居民端「查看全过程」只弹机构与轨迹；`static/` 下没有任何地方渲染这两项。审核人看不到规则开单命中
了哪几条、交了什么资料，居民也看不到（需求对照表居民端 #16「查看触发依据、异常指标、提交资料」）。

修后三处各补一段「触发依据」与「转诊资料」，经 shared.js 同一处帮手：依据逐条转成「字段 比较符 阈值」（中文名取
`GET /api/spd/meta`，居民端调不到它、只认首页那几项指标名），资料链接只给 http(s) 画（同 P2-1465），其余文字一律 esc()。
居民端是 alert 的纯文本，不经 innerHTML。上传入口与「依据存取值快照」不在本条（登记待裁定）。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
SHARED = (STATIC / "shared.js").read_text(encoding="utf-8")

EVIDENCE = {"matched": [
    {"field": "bp_sys", "op": ">=", "value": 180, "label": ""},
    {"field": "diagnosis", "op": "in", "value": ["I10", "<b>I15</b>"], "label": "确诊<高血压>"},
    {"field": "glucose_fasting", "op": "between", "value": [7, 11.1], "label": ""},
]}
HTTPS_URL = "https://cdn.example/bp.pdf?a=1&b=2"
MATERIALS = [
    {"name": "近3月血压<记录>", "url": HTTPS_URL, "note": "家庭自测"},
    {"name": "恶意链接", "url": "javascript:alert(document.cookie)"},
    {"title": "心电图", "link": "JAVASCRIPT:alert(1)"},
    {"attachment_id": 7},
]
#: 依据逐条的人话（中文名取真接口 /api/spd/meta：后端 rules.FIELD_SOURCES / OPERATORS）
LINES = ["收缩压 大于等于 180", "诊断编码 属于 I10、<b>I15</b>（确诊<高血压>）", "空腹血糖 介于 7 ~ 11.1"]


def _esc(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _function(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 2]


def _node(script: str, *args) -> str:
    if shutil.which("node") is None:
        pytest.skip("没有 node 执行前端渲染")
    done = subprocess.run(["node", "-e", script, *args], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


PRELUDE = "globalThis.document = { addEventListener() {}, querySelector() { return null; }, cookie: '' };\n" + SHARED


@pytest.fixture(scope="module")
def meta(client, admin):
    got = client.get("/api/spd/meta", headers=admin)
    assert got.status_code == 200, got.text
    return {"fields": got.json()["fields"], "operators": got.json()["operators"]}


def _assert_materials_html(html: str) -> None:
    assert f'<a href="{_esc(HTTPS_URL)}" target="_blank" rel="noopener">{_esc("近3月血压<记录>")}</a>（家庭自测）' in html
    assert html.count("<a ") == 1 and html.count("href=") == 1, html   # javascript: 两条不画成链接
    assert "恶意链接 javascript:alert(document.cookie)" in html and "心电图 JAVASCRIPT:alert(1)" in html
    assert _esc('{"attachment_id":7}') in html   # 认不出的结构照 JSON 印、不吞掉
    assert "<记录>" not in html and "<b>" not in html


# ---------------------------------------------------------------- 管理端：全轨迹上方两段


def _admin(case: dict, meta) -> str:
    pages = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    script = (PRELUDE + _function(pages, "function spdReferralBasis(")
              + "const [c, meta] = JSON.parse(process.argv[1]);\n"
              + "process.stdout.write(spdReferralBasis(c, meta));\n")
    return _node(script, json.dumps([case, meta], ensure_ascii=False))


def test_管理端全轨迹画出触发依据与转诊资料_经esc(meta):
    html = _admin({"trigger_rule_code": "R<1>", "trigger_evidence": EVIDENCE, "materials": MATERIALS}, meta)
    basis = html.split("触发依据", 1)[1].split("</p>", 1)[0]
    assert basis == f"（规则 {_esc('R<1>')}）：" + "；".join(_esc(x) for x in LINES), html   # 修前没有这一段
    _assert_materials_html(html.split("转诊资料：", 1)[1])


def test_管理端两项都空写横杠(meta):
    html = _admin({"trigger_rule_code": "", "trigger_evidence": {}, "materials": []}, meta)
    assert "触发依据：—" in html and "转诊资料：—" in html, html


def test_管理端全轨迹接上了这两段():
    pages = strip_comments((STATIC / "pages-spd.js").read_text(encoding="utf-8"))
    branch = pages[pages.index("if (detail) {", pages.index("async function renderSpdReferral(")):]
    branch = branch[:branch.index("    if (withdraw) {")]
    assert "spdMeta().catch(() => null)" in branch, branch
    assert "spdReferralBasis(c, meta) +" in branch, branch   # 修前 panel 里只有轨迹表


# ---------------------------------------------------------------- 医生端：转诊卡片


def _doctor_cards(rows: list, meta) -> str:
    doctor = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    script = (
        PRELUDE
        + "let spdMe = { id: 9, is_village_doctor: false };\n"
        + _function(doctor, "function kv(") + _function(doctor, "function spdReferralOps(")
        + _function(doctor, "function spdReferralBasis(") + _function(doctor, "async function loadSpdReferral(")
        + "const [ROWS, META] = JSON.parse(process.argv[1]);\n"
        "async function api(path) {\n"
        "  if (path.startsWith('/api/spd/referrals?open_only=true')) return JSON.parse(JSON.stringify(ROWS));\n"
        "  if (path === '/api/spd/meta') return META;\n"
        "  return [];\n"
        "}\n"
        "const stub = { addEventListener() {} };\n"
        "const box = { innerHTML: '', querySelector() { return stub; }, querySelectorAll() { return []; } };\n"
        "(async () => { await loadSpdReferral(box); process.stdout.write(box.innerHTML); })()\n"
        "  .catch((err) => { console.error(err); process.exit(1); });\n"
    )
    return _node(script, json.dumps([rows, meta], ensure_ascii=False))


def _row(rid: int, **extra) -> dict:
    return {"id": rid, "patient_name": f"患者{rid}", "direction": "up", "status": "submitted", "reason": "血压控制差",
            "actions": ["review"], "trigger_evidence": {}, "materials": [], **extra}


def test_医生端转诊卡片画出触发依据与转诊资料_经esc(meta):
    out = _doctor_cards([_row(1, trigger_evidence=EVIDENCE, materials=MATERIALS), _row(2)], meta)
    first, second = out.split('<div class="m-card">')[1:3]   # 「发起上转」那张带 id，不在切分里
    expected = "；".join(_esc(x) for x in LINES)
    assert f'<span class="k">触发依据</span><span>{expected}</span>' in first, first   # 修前卡片只有理由
    _assert_materials_html(first.split('<span class="k">转诊资料</span>', 1)[1])
    assert "触发依据" not in second and "转诊资料" not in second, second   # 两项都空不出这一行
    assert 'data-spd-pass="1"' in first   # 审核按钮照旧


def test_医生端取不到元数据照原样印编码():
    out = _doctor_cards([_row(1, trigger_evidence={"matched": [EVIDENCE["matched"][0]]})], None)
    assert '<span class="k">触发依据</span><span>bp_sys &gt;= 180</span>' in out, out


# ---------------------------------------------------------------- 居民端：「查看全过程」


def _resident_alert(detail: dict) -> str:
    m_js = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    start = m_js.index("const SPD_METRIC_NAMES = ")
    script = (
        PRELUDE
        + m_js[start:m_js.index("\n", start) + 1]
        + _function(m_js, "function bindReferralDetails(")
        + "const DETAIL = JSON.parse(process.argv[1]);\n"
        "async function authApi() { return JSON.parse(JSON.stringify(DETAIL)); }\n"
        "let shown = null;\n"
        "globalThis.alert = (text) => { shown = text; };\n"
        "let handler = null;\n"
        "const btn = { dataset: { refDetail: '/api/portal/spd/referrals/1' }, addEventListener(_t, fn) { handler = fn; } };\n"
        "bindReferralDetails({ querySelectorAll() { return [btn]; } });\n"
        "(async () => { await handler(); process.stdout.write(shown); })()\n"
        "  .catch((err) => { console.error(err); process.exit(1); });\n"
    )
    return _node(script, json.dumps(detail, ensure_ascii=False))


def _detail(**extra) -> dict:
    return {"id": 1, "direction": "up", "status": "submitted", "current_level": "village", "reason": "血压控制差",
            "trigger_evidence": {}, "materials": [], "from_org": "东村卫生室", "to_org": "县医院", "down_to_org": "",
            "steps": [{"step": "发起", "action": "submit", "opinion": "", "created_at": "2026-10-09T08:00:00",
                       "action_name": "发起"}], **extra}


def test_居民端全过程弹出触发依据与转诊资料_纯文本不画链接():
    shown = _resident_alert(_detail(trigger_evidence=EVIDENCE, materials=MATERIALS))
    # 居民端调不到 /api/spd/meta：首页认得的指标（收缩压、空腹血糖）用中文名，其余照原样印编码；alert 是纯文本，不转义
    assert ("触发依据：收缩压 >= 180；diagnosis in I10、<b>I15</b>（确诊<高血压>）；空腹血糖 between 7 ~ 11.1\n"
            in shown), shown   # 修前只有机构与轨迹
    assert ("转诊资料：近3月血压<记录> " + HTTPS_URL + "（家庭自测）；恶意链接 javascript:alert(document.cookie)；"
            '心电图 JAVASCRIPT:alert(1)；{"attachment_id":7}\n') in shown, shown
    assert shown.startswith("转出：东村卫生室　转入：县医院\n触发依据：") and shown.endswith("发起")


def test_居民端两项都空不出这两行():
    shown = _resident_alert(_detail())
    assert "触发依据" not in shown and "转诊资料" not in shown, shown
    assert shown == "转出：东村卫生室　转入：县医院\n2026-10-09 08:00 发起"


def test_三端走shared_js同一处帮手_不再各写一份():
    shared = strip_comments(SHARED)
    for head in ("function referralEvidenceLines(", "function referralMaterialsHtml(", "function referralMaterialLines("):
        assert head in shared
    assert "isHttpUrl(m.url)" in _function(shared, "function referralMaterialsHtml(")
    for path in ("pages-spd.js", "m/doctor.js", "m/m.js"):
        source = strip_comments((STATIC / path).read_text(encoding="utf-8"))
        assert "referralEvidenceLines(" in source, path
        assert "function referralEvidenceLines(" not in source and "/^https?:\\/\\//i" not in source, path
