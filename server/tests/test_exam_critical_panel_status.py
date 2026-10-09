"""共享诊断页的「⚠ 危急值（N）」把已处置的也算进去，表里也没有闭环状态列（P2-1713，第五十批扫描 AN2-7）。

`core.js renderExams` 的危急值面板取 `GET /api/exams/critical`——这张清单把已处置的排在后面、但不过滤（P1-166）。面板标题原先
直接写 `critical.length`，表里只有报告ID / 申请单 / 结论 / 操作，已处置的结论与待确认的同样是红标签：全县出过一条危急值，这个
「⚠」面板就永远挂着、计数只增不减（按代码读，同一份数据在危急值操作台带「闭环状态」列）。

修法：标题只数未处置的（待确认 + 已确认待反馈，存量空串等同已通知），一条都没有时不挂「⚠」；表里加「闭环状态」一列，状态文案取
危急值操作台同一张 `CRIT_STATUS`；已处置的结论标签不再标红。这一页的清单仍是缺省那一页（P2-1711 只改了两个危急值页）。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import ExamReport, ExamRequest, User

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


@pytest.fixture(scope="module")
def critical_rows(client, admin):
    """两条未处置（待确认、已确认待反馈）、一条已处置的危急值，取真接口的清单行。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21713 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21713 患者", "id_card": "330127197303031713"}).json()["id"]
    with SessionLocal() as db:
        creator = db.query(User).filter_by(username="admin").one().id
        for i, status in enumerate(("notified", "acknowledged", "resolved")):
            req = ExamRequest(patient_id=patient, from_org_id=org, center_type="lab", item_code=f"P21713-{i}",
                              item_name="血钾", status="reported", created_by=creator)
            db.add(req)
            db.flush()
            db.add(ExamReport(request_id=req.id, conclusion=f"P21713 血钾<{status}>", critical=True, critical_status=status))
        db.commit()
    rows = client.get("/api/exams/critical", headers=admin).json()
    assert sorted(r["critical_status"] for r in rows) == ["acknowledged", "notified", "resolved"]
    return rows


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


def _const(source: str, name: str) -> str:
    start = source.index(f"const {name} = ")
    return source[start:source.index(";\n", start) + 2]


#: 页面取数换成桩：危急值清单回给定的行，其余清单一律空
_HARNESS = """
const els = {};
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", value: "", dataset: {},
    classList: { add() {}, remove() {} }, addEventListener() {} }); } };
const CRITICAL = JSON.parse(process.argv[1]);
async function api(path) { return path === "/api/exams/critical" ? CRITICAL : []; }
"""


def _render(rows) -> str:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    clinical = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function actionableFirst(") + _top_level(core, "function setMsg(")
              + "".join(_const(core, name) for name in ("CENTER_NAMES", "EXAM_STATUS", "SAMPLE_NEXT", "SAMPLE_STATUS"))
              + _const(clinical, "CRIT_STATUS") + _top_level(core, "async function renderExams()")
              + "renderExams().then(() => process.stdout.write(els['#page-body'].innerHTML));\n")
    out = subprocess.run(["node", "-e", script, json.dumps(rows, ensure_ascii=False)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _panel(html: str) -> str:
    start = html.index("危急值（")
    return html[html.rindex('<div class="panel"', 0, start):html.index("</table>", start)]


def test_标题只数未处置的_表里有闭环状态列(critical_rows):
    panel = _panel(_render(critical_rows))
    assert "<h3>⚠ 危急值（未处置 2）</h3>" in panel, panel   # 修前「⚠ 危急值（3）」：已处置的也算进去
    assert "<th>闭环状态</th>" in panel   # 修前没有这一列
    rows = re.findall(r"<tr><td>(\d+)</td>.*?</tr>", panel, re.S)
    assert [int(x) for x in rows] == [r["id"] for r in critical_rows]
    labels = {"notified": "已通知", "acknowledged": "已确认", "resolved": "已处置"}   # 同危急值操作台的 CRIT_STATUS
    for r in critical_rows:
        row = panel[panel.index(f"<tr><td>{r['id']}</td>"):]
        row = row[:row.index("</tr>")]
        assert f">{labels[r['critical_status']]}</span></td>" in row, row
        # 结论照旧转义；已处置的不再标红，未处置的仍是红标签
        tag = "tag" if r["critical_status"] == "resolved" else "tag red"
        assert f'<span class="{tag}">P21713 血钾&lt;{r["critical_status"]}&gt;</span>' in row, row


def test_全部已处置_标题未处置0_不挂警示(critical_rows):
    resolved = [{**r, "critical_status": "resolved"} for r in critical_rows]
    panel = _panel(_render(resolved))
    assert "<h3>危急值（未处置 0）</h3>" in panel, panel   # 修前「⚠ 危急值（3）」永远挂着
    assert 'class="tag red"' not in panel


def test_存量空串按未处置数_写已通知(critical_rows):
    legacy = [{**critical_rows[0], "critical_status": ""}]
    panel = _panel(_render(legacy))
    assert "<h3>⚠ 危急值（未处置 1）</h3>" in panel, panel
    assert ">已通知</span></td>" in panel
