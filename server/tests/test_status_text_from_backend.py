"""工作人员端把英文状态码原样显示：状态文案取自后端（P2-72，§13 功能完整第 7 项）。

2026-09-25 按形状全仓扫出 14 处：页面单元格里直接 `${esc(x.status)}`，或在三元里只译一两个值、其余
`'<span class="tag">' + esc(x.status)` 兜底——药剂科看到的制剂批次状态是 `produced`，急救调度看到
`en_route`，随访看板上失访的记录显示 `unreachable`，健康日历里的随访 / 复诊 / 任务一个都不译。
特病申报队列那一处还是裸插值 `${a.status}`（值只由服务端写，眼下不可利用，但违反 §8「innerHTML 插值
不得漏转义」）。

修法：出参补后端给的 `status_name`（与既有 `MAP.get(code, code)` 惯例一致：表外的值原样回显，
「后端加了新状态、表还没跟上」的现场看得见），页面改显示它；同一文件里已有同一套状态文案表的
（慢专病复诊、任务）直接复用那张表，不另起一份措辞。

本文件：
- 前端闸门：页面文字里不许出现原样的状态码插值（派生零基线 + 按设计名单只减不增，每条写理由）；
- 文案表对照列注释：每张新文案表的键与模型列注释里列出的状态码一一对应——加了新状态没补文案，
  这里先红（否则页面上又是一个英文码）；
- 端点回归：没有契约网钉着的三处（急救事件、特病申报、报告修订回执）走一遍状态流转。
"""
import inspect
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

from test_frontend_escape_guard import _strip_comments  # 同一份注释剥离：块注释换等量换行，行号不错位

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

#: 零基线（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-25 实测（修前 377c0fe）：页面文字里原样的状态码插值 18 处，按设计 3 → 其余 15 处已改显示后端文案
#: （初版判据漏了带兜底的写法，量出 17；补上后多认出凭证核验页那一处）。
BASELINE = 0

#: 按设计原样显示的（`(文件, 插值)` → 理由）。**只减不增**；每条只抵一处——同一文件再添一句同样的插值照样红。
BY_DESIGN = {
    ("pages-mgmt.js", "esc(i.raw_status)"):
        "统一在办事项的「原生状态」一栏：刻意并排给出各业务表自己的状态码供对照排查，同一行「统一状态」才是给人读的文案",
    ("pages-mgmt.js", "${r.status}"):
        "运行监控页错误样本的 HTTP 状态码（数字），不是业务状态",
    ("pages-public.js", "${x.status}"):
        "ESB 流程执行记录的步骤结果快照（`步骤.类型=结果`），给对接工程师排障的技术串，整串已 esc()",
}

_FIELD = r"([A-Za-z_$][\w$]*)\.(?:[a-z_]*_)?status"
#: 把状态码原样放进页面的写法：`esc(x.status)`（插值或字符串拼接里）、裸插值 `${x.status}`、两者带兜底
#: （`${x.status || "未知"}` / `esc(x.status ?? "")`——有值时显示的照样是原码）、裸拼接 `+ x.status +`。
#: `status_name` / `status_code` 不命中（`status` 后面必须紧跟右括号、右花括号、兜底运算符或加号）。
#: 兜底那两种是后补的（初版漏了，凭证核验页「失效（void）」就是这个形状）。
RAW_STATUS = re.compile(
    rf"(?:esc\(|\$\{{)\s*{_FIELD}\s*(?:\)|\}}|\|\||\?\?)"
    rf"|\+\s*{_FIELD}\s*\+"
)
#: 同一行内双引号包着的属性值（`data-status="${esc(t.status)}"`）：取值进属性、给脚本回读，不是页面文字
ATTR_VALUE = re.compile(r'[\w-]+="[^"\n]*"')
#: fetch 的 Response：`resp.status` 是 HTTP 状态码
HTTP_RESPONSE_NAMES = {"resp"}


def raw_status_displays(files=None) -> list[tuple[str, str, int]]:
    """`(文件, 插值, 行号)`：页面文字里原样的状态码插值。"""
    found = []
    for path in files if files is not None else sorted(STATIC.rglob("*.js")):
        label = path.relative_to(STATIC).as_posix() if path.is_relative_to(STATIC) else path.name
        for lineno, line in enumerate(_strip_comments(path.read_text(encoding="utf-8")).splitlines(), 1):
            for m in RAW_STATUS.finditer(ATTR_VALUE.sub('""', line)):
                owner = next(g for g in m.groups() if g)
                if owner not in HTTP_RESPONSE_NAMES:
                    found.append((label, m.group(0), lineno))
    return found


def _minus_by_design(found):
    budget = Counter(BY_DESIGN.keys())
    rest = []
    for label, snippet, lineno in found:
        if budget[(label, snippet)] > 0:
            budget[(label, snippet)] -= 1
        else:
            rest.append(f"{label}:{lineno}: {snippet}")
    return rest


# ================================================================ 前端闸门
def test_页面上不原样显示状态码():
    bad = _minus_by_design(raw_status_displays())
    assert len(bad) <= BASELINE, (
        "这些地方把后端的状态码原样显示给用户（§13 第 7 项：状态文案取自后端）：\n  " + "\n  ".join(bad)
        + "\n\n出参补 `status_name`（后端文案表 `MAP.get(code, code)`）并显示它；同一文件已有同一套状态的文案表就复用；"
        "确实该显示原码的（技术串、HTTP 状态码），进 BY_DESIGN 写明理由。"
    )


def test_按设计名单只减不增():
    present = Counter((label, snippet) for label, snippet, _ in raw_status_displays())
    stale = sorted(f"{label}: {snippet}" for label, snippet in BY_DESIGN if present[(label, snippet)] == 0)
    assert stale == [], "这些已经改掉或挪走了，请从 BY_DESIGN 划掉：\n  " + "\n  ".join(stale)


def test_判据自证_三种写法都认得出_写对的放过(tmp_path):
    probe = tmp_path / "probe.js"
    probe.write_text(
        # 应命中的三种
        "a = `<td>${esc(b.status)}</td>`;\n"
        "c = `<td>${d.critical_status}</td>`;\n"
        "e = '<span class=\"tag\">' + esc(f.status) + '</span>' + g.status + '';\n"
        "s = `<b>${esc(t.status || \"—\")}</b><b>${u.credential_status ?? \"未知\"}</b>`;\n"
        # 应放过的：后端文案、查表组件、属性值、HTTP 状态码、别的字段、注释
        "h = `<td>${esc(i.status_name)}</td><td>${statusTag(MAP, j.status)}</td>`;\n"
        "k = `<button data-status=\"${esc(l.status)}\">改</button>`;\n"
        "m = new Error(`请求失败(${resp.status})`);\n"
        "n = `<td>${o.status_code}</td><td>${esc(p.status_text)}</td>`;\n"
        "// q = `<td>${esc(r.status)}</td>`;\n",
        encoding="utf-8",
    )
    hits = [(snippet, lineno) for _, snippet, lineno in raw_status_displays([probe])]
    assert hits == [("esc(b.status)", 1), ("${d.critical_status}", 2),
                    ("esc(f.status)", 3), ("+ g.status +", 3),
                    ("esc(t.status ||", 4), ("${u.credential_status ??", 4)], hits


def test_判据自证_同一插值每条豁免只抵一处(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "BY_DESIGN", {("probe.js", "${r.status}"): "理由"})
    probe = tmp_path / "probe.js"
    probe.write_text("a = `<td>${r.status}</td>`;\nb = `<td>${r.status}</td>`;\n", encoding="utf-8")
    assert _minus_by_design(raw_status_displays([probe])) == ["probe.js:2: ${r.status}"]


def test_判据自证_扫描面覆盖三端():
    """防空转：认不出任何状态插值时闸门会空转成绿——三端源码里状态字段的引用必须扫得到。"""
    files = {p.relative_to(STATIC).as_posix() for p in STATIC.rglob("*.js")}
    assert {"core.js", "pages-spd.js", "m/m.js", "m/doctor.js"} <= files
    total = sum(p.read_text(encoding="utf-8").count(".status") for p in STATIC.rglob("*.js"))
    assert total >= 300, total


# ================================================================ 文案表对照列注释
def _column_codes(model, column: str) -> set[str]:
    """模型源码里 `column: Mapped…` 上方那行注释列出的状态码（`code=文案` 形状，`""` 也算一个）。"""
    lines = inspect.getsource(model).splitlines()
    at = next(i for i, line in enumerate(lines) if re.match(rf"\s+{column}: Mapped", line))
    comment = lines[at - 1].strip()
    assert comment.startswith("#"), f"{model.__name__}.{column} 上方没有列出状态码的注释"
    return {"" if code == '""' else code for code in re.findall(r'(""|[a-z][a-z_]*)=', comment)}


def _label_tables():
    from app.models import (Admission, EmergencyCase, ExamReport, SpecialDiseaseApp, SpdCenter,
                            SpdFollowupRecord, TcmPreparationBatch, TrainingEnrollment, TrainingPlan,
                            VisitCredential)
    from app.routers import credentials, education, emergency, exams, insurance, tcm
    from app.spd.routers import followup, workbench

    # (文案表, 模型, 列, 按设计不进表的码)
    return {
        "emergency.CASE_STATUS_NAMES": (emergency.CASE_STATUS_NAMES, EmergencyCase, "status", set()),
        "tcm.BATCH_STATUS_NAMES": (tcm.BATCH_STATUS_NAMES, TcmPreparationBatch, "status", set()),
        "education.PLAN_STATUS_NAMES": (education.PLAN_STATUS_NAMES, TrainingPlan, "status", set()),
        "education.ENROLLMENT_STATUS_NAMES":
            (education.ENROLLMENT_STATUS_NAMES, TrainingEnrollment, "status", set()),
        "insurance.SPECIAL_DISEASE_STATUS_NAMES":
            (insurance.SPECIAL_DISEASE_STATUS_NAMES, SpecialDiseaseApp, "status", set()),
        # 空串 = 非危急值，没有闭环状态，文案也是空串
        "exams.CRITICAL_STATUS_NAMES": (exams.CRITICAL_STATUS_NAMES, ExamReport, "critical_status", {""}),
        "workbench.CENTER_STATUS_NAMES": (workbench.CENTER_STATUS_NAMES, SpdCenter, "status", set()),
        "followup.FOLLOWUP_STATUS_NAMES":
            (followup.FOLLOWUP_STATUS_NAMES, SpdFollowupRecord, "status", set()),
        "followup.ADMISSION_STATUS_NAMES": (followup.ADMISSION_STATUS_NAMES, Admission, "status", set()),
        # 既有的表，本批起凭证核验回执也用它
        "credentials.STATUS_NAMES": (credentials.STATUS_NAMES, VisitCredential, "status", set()),
    }


#: 本批新建或新用上的文案表（`_label_tables` 的键）；参数化用静态名单，收集阶段不导入应用
LABEL_TABLE_NAMES = [
    "emergency.CASE_STATUS_NAMES", "tcm.BATCH_STATUS_NAMES", "education.PLAN_STATUS_NAMES",
    "education.ENROLLMENT_STATUS_NAMES", "insurance.SPECIAL_DISEASE_STATUS_NAMES", "exams.CRITICAL_STATUS_NAMES",
    "workbench.CENTER_STATUS_NAMES", "followup.FOLLOWUP_STATUS_NAMES", "followup.ADMISSION_STATUS_NAMES",
    "credentials.STATUS_NAMES",
]


def test_文案表名单与参数化一致():
    assert sorted(_label_tables()) == sorted(LABEL_TABLE_NAMES)


@pytest.mark.parametrize("name", LABEL_TABLE_NAMES)
def test_文案表覆盖列注释里的每个状态码(name):
    table, model, column, skipped = _label_tables()[name]
    codes = _column_codes(model, column)
    assert codes, f"{model.__name__}.{column} 的注释里认不出状态码"
    assert set(table) == codes - skipped, (
        f"{name} 与 {model.__name__}.{column} 列注释对不上：缺 {sorted(codes - skipped - set(table))}，"
        f"多 {sorted(set(table) - codes)}——加了新状态要同时补文案，否则页面上又是一个英文码"
    )
    assert all(text and text.strip() for text in table.values()), name


# ================================================================ 端点回归
def test_急救事件带状态文案_随流转而变(client, admin):
    case = client.post("/api/emergency/cases", headers=admin, json={"location": "P272 某路口"})
    assert case.status_code == 201, case.text
    case = case.json()
    assert (case["status"], case["status_name"]) == ("dispatched", "已调度")   # 修前：没有 status_name
    names = []
    for _ in range(3):
        moved = client.post(f"/api/emergency/cases/{case['id']}/advance", headers=admin)
        assert moved.status_code == 200, moved.text
        names.append(moved.json()["status_name"])
    assert names == ["转运中", "已到院", "已收治"]
    judged = client.post(f"/api/emergency/cases/{case['id']}/rescue-outcome", headers=admin,
                         json={"rescue_outcome": "success"})
    assert judged.status_code == 200, judged.text
    assert judged.json()["status_name"] == "已收治"
    row = next(c for c in client.get("/api/emergency/cases", headers=admin).json() if c["id"] == case["id"])
    assert (row["status"], row["status_name"]) == ("admitted", "已收治")


def test_特病申报带状态文案_审核后随之而变(client, admin):
    patient = client.post("/api/patients", headers=admin,
                          json={"name": "P272 特病患者", "id_card": "330192198001014321"}).json()
    applied = client.post("/api/insurance/special-diseases", headers=admin,
                          json={"patient_id": patient["id"], "disease_name": "P272 尿毒症透析"})
    assert applied.status_code == 201, applied.text
    applied = applied.json()
    assert applied["status_name"] == "已申报"   # 修前：页面显示 applied（且是裸插值）
    reviewed = client.post(f"/api/insurance/special-diseases/{applied['id']}/review",
                           params={"approve": "true"}, headers=admin)
    assert reviewed.status_code == 200, reviewed.text
    assert (reviewed.json()["status"], reviewed.json()["status_name"]) == ("approved", "已批准")
    row = next(a for a in client.get("/api/insurance/special-diseases", headers=admin).json()
               if a["id"] == applied["id"])
    assert row["status_name"] == "已批准"
