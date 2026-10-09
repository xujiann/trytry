"""随访看板对失访的记录给「补录」（P2-1637，第四十八批扫描 AL1-4 页面那一半）。

后端执行随访收失访（`execute_followup` 的 `allowed_from=("planned", "overdue", "unreachable")`；`service.py` 与
`followup.py` 的注释都写「失访的还能补录」），页面随访看板却只对待随访、已超期的给「执行」——失访记录要先「调整 →
恢复为待随访」才能补录。修后看板的「执行」与后端可执行的状态同一个集合（`SPD_FU_EXECUTABLE`），失访行的按钮写「补录」；
「转呼叫」与接通回写不动（接通结果进不进随访记录是 P2-1256 待裁定）。界面上的点法由 e2e
`test_失访的随访在看板上能补录` 覆盖。
"""
import ast
import inspect
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

B = "/api/spd"
SRC = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = SRC.index(f"function {name}(")
    return SRC[start:SRC.index("\n}\n", start) + 2]


def _executable_in_page() -> list[str]:
    match = re.search(r"^const SPD_FU_EXECUTABLE = (\[.*?\]);$", SRC, re.M)
    assert match, "pages-spd.js 里找不到 SPD_FU_EXECUTABLE"
    return json.loads(match.group(1))


def test_看板给执行的状态与后端可执行的来源态同一个集合():
    from app.spd.routers import followup

    tree = ast.parse(inspect.getsource(followup.execute_followup).strip())
    (allowed,) = [kw.value for node in ast.walk(tree) if isinstance(node, ast.Call)
                  for kw in node.keywords if kw.arg == "allowed_from"]
    assert _executable_in_page() == list(ast.literal_eval(allowed))   # 修前页面只认 planned / overdue


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_失访行给补录_不给转呼叫():
    script = ("const SPD_FU_EXECUTABLE = " + json.dumps(_executable_in_page()) + ";\n"
              + _function("spdFollowupRowActions")
              + "\nconst rows = JSON.parse(process.argv[1]);"
              + "\nconsole.log(JSON.stringify(rows.map(spdFollowupRowActions)));")
    rows = [{"id": i, "patient_id": 7, "status": s}
            for i, s in enumerate(("planned", "overdue", "unreachable", "done", "removed"), start=1)]
    out = json.loads(subprocess.run(["node", "-e", script, json.dumps(rows)], capture_output=True, text=True,
                                    check=True, timeout=60).stdout)
    call = '<button class="btn secondary" data-fu-call="{}" data-pid="7">转呼叫</button>'
    assert out == [
        f'<button class="btn secondary" data-fu-exec="1">执行</button> {call.format(1)}',
        f'<button class="btn secondary" data-fu-exec="2">执行</button> {call.format(2)}',
        '<button class="btn secondary" data-fu-exec="3">补录</button>',   # 修前失访行什么都没有
        "", "",
    ]
    # 看板行确实走这个帮手
    draw = SRC[SRC.index("const drawRecords = async (query) => {"):]
    assert "${spdFollowupRowActions(r)}" in draw[:draw.index("$(\"#spd-furule-form\")")]


def test_接口从失访补录照旧200_结果追加(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21637 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21637 患者", "id_card": "330102196001011637"}).json()["id"]
    rule = client.post(f"{B}/followup-rules", headers=admin, json={"code": "P21637_FR", "name": "P21637 随访", "points": [0]})
    assert rule.status_code == 201, rule.text
    plan = client.post(f"{B}/followup-plans", headers=admin, json={
        "patient_id": patient, "rule_id": rule.json()["id"], "org_id": org})
    assert plan.status_code == 201, plan.text
    record = plan.json()["items"][0]["id"]
    lost = client.post(f"{B}/followup-records/{record}/execute", headers=admin,
                       json={"result": "三次未接通", "unreachable": True})
    assert lost.status_code == 200 and lost.json()["status"] == "unreachable", lost.text
    done = client.post(f"{B}/followup-records/{record}/execute", headers=admin, json={"result": "患者回电，恢复良好"})
    assert done.status_code == 200, done.text
    assert (done.json()["status"], done.json()["result"]) == ("done", "三次未接通 患者回电，恢复良好")
