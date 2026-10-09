"""路径实例四处只印编码：「当前节点」印节点键，档案详情与 360 卡印模板编码，360 卡连状态都不印（P2-1600，第四十七批
扫描 AK3-7）。

修前：路径实例清单 / 执行明细的出参（`PathInstanceOut`）只有 `current_node_key`，纳管档案详情与患者 360 的 `paths` 只有
`template_code` 与 `current_node_key`——页面「当前节点」一栏印 `n1`、`assess`，360 卡印 `#1 AK3P n1 25%`，已取消的路径与
执行中的长得一样。居民端早按 P2-372 回了名称，执行明细的节点表印的也是名称。

修法：出参只在末尾追加——实例出参加 `current_node_name`（清单按页一次取齐），档案详情与 360 的路径行加 `template_name`、
`current_node_name`；页面四处印名称（名称为空回落到编码），360 卡补状态中文（`SPD_INST_STATUS`）。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from jssrc import strip_comments

from app.database import SessionLocal

B = "/api/spd"
SRC = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
CODE, NAME = "P21600_PATH", "P21600 高血压规范管理"


def _function(name: str) -> str:
    start = SRC.index(f"function {name}(")
    return SRC[start:SRC.index("\n}\n", start) + 2]


def _const(name: str) -> str:
    start = SRC.index(f"const {name} = {{")
    return SRC[start:SRC.index("};", start) + 2]


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdPathNode, SpdPathTemplate, SpdProgram

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21600 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code=CODE, name=NAME, status="published")
        db.add(template)
        db.flush()
        db.add_all([SpdPathNode(template_id=template.id, key="n1", name="首诊评估", stage="s1", seq=1),
                    SpdPathNode(template_id=template.id, key="n2", name="强化管理", seq=2)])
        db.commit()
        template_id = template.id
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21600 患者", "id_card": "330127197309091600"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert enrollment.status_code == 201, enrollment.text
    started = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": enrollment.json()["id"], "template_id": template_id})
    assert started.status_code == 201, started.text
    return {"patient": patient, "enrollment": enrollment.json()["id"], "instance": started.json()}


def _get(client, admin, path, **params):
    resp = client.get(path, headers=admin, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_实例出参在末尾追加当前节点名(client, admin, world):
    created = world["instance"]
    assert list(created)[-1] == "current_node_name"   # 只追加在末尾，前面的键序不动
    assert created["current_node_key"] == "n1" and created["current_node_name"] == "首诊评估"   # 修前没有这个键
    [row] = _get(client, admin, f"{B}/path-instances", enrollment_id=world["enrollment"])
    assert row == created   # 清单按页取齐的与单条出参同一个值
    detail = _get(client, admin, f"{B}/path-instances/{created['id']}")
    assert detail["current_node_name"] == "首诊评估" and list(detail)[-2:] == ["current_node_name", "nodes"]


def test_档案详情与360的路径行带模板名与节点名(client, admin, world):
    [path] = _get(client, admin, f"{B}/enrollments/{world['enrollment']}")["paths"]
    assert list(path) == ["id", "template_code", "status", "current_node_key", "current_stage", "progress",
                          "template_name", "current_node_name"]
    assert (path["template_name"], path["current_node_name"]) == (NAME, "首诊评估")   # 修前只有编码
    [card] = _get(client, admin, f"{B}/patients/{world['patient']}/profile")["programs"]
    [path] = card["paths"]
    assert list(path) == ["id", "template_code", "status", "current_node_key", "progress",
                          "template_name", "current_node_name"]
    assert (path["template_name"], path["current_node_name"]) == (NAME, "首诊评估")


def test_页面四处不再直接印节点键与模板编码():
    src = strip_comments(SRC)
    for raw in ("esc(i.current_node_key", "esc(inst.current_node_key", "esc(i.template_code"):
        assert raw not in src, raw   # 修前四处都这样直接印
    for name in ("spdProfileHtml", "spdEnrollmentDetailHtml"):
        body = strip_comments(_function(name))
        assert "i.current_node_name || i.current_node_key" in body and "i.template_name || i.template_code" in body
    assert "inst.current_node_name || inst.current_node_key" in strip_comments(_function("spdInstanceDetailHtml"))
    listing = src[src.index('$("#spd-inst-list").innerHTML'):]
    assert "i.current_node_name || i.current_node_key" in listing[:listing.index("</tr>`")]
    assert "SPD_INST_STATUS[i.status]" in strip_comments(_function("spdProfileHtml"))   # 360 卡补状态中文


STUBS = ("const esc = (s) => String(s ?? \"\");\nconst spdTag = () => \"\";\n"
         "const table = (heads, rows, fn) => rows.map(fn).join(\"\");\nconst panel = (title, body) => body;\n"
         "const spdBindingTag = () => \"\";\n"
         "const SPD_RISK = {}, SPD_TASK_STATUS = {}, SPD_MEAS_LEVEL = {}, SPD_REF_STATUS = {};\n")


def _render(function: str, payload) -> str:
    script = (STUBS + _const("SPD_ENROLL_STATUS") + "\n" + _const("SPD_INST_STATUS") + "\n" + _function(function)
              + f"\nconsole.log({function}(JSON.parse(process.argv[1]), {{}}));")
    return subprocess.run(["node", "-e", script, json.dumps(payload, ensure_ascii=False)],
                          capture_output=True, text=True, check=True, timeout=60).stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_跑一遍_四处印名称_360卡印状态_名称为空回落到编码(client, admin, world):
    profile = _get(client, admin, f"{B}/patients/{world['patient']}/profile")
    line = next(row for row in _render("spdProfileHtml", profile).splitlines() if "路径：" in row)
    assert NAME in line and "首诊评估" in line and "执行中" in line, line   # 修前「#1 P21600_PATH n1 0%」
    assert CODE not in line and " n1 " not in line, line
    profile["programs"][0]["paths"][0].update(template_name="", current_node_name="")
    line = next(row for row in _render("spdProfileHtml", profile).splitlines() if "路径：" in row)
    assert CODE in line and " n1 " in line, line   # 名称为空回落到编码

    enrollment = _get(client, admin, f"{B}/enrollments/{world['enrollment']}")
    html = _render("spdEnrollmentDetailHtml", enrollment)
    assert f"<td>{NAME}</td>" in html and "<td>首诊评估</td>" in html and CODE not in html, html

    detail = _get(client, admin, f"{B}/path-instances/{world['instance']['id']}")
    assert "当前节点 首诊评估" in _render("spdInstanceDetailHtml", detail)   # 修前「当前节点 n1」
