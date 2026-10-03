"""专病 360 档案卡片对已登记死亡 / 迁出的档案照印「下次随访」（P2-1199，第三十四批「患者全景与时间轴」扫描 L4-7）。

随访办结后档案排上了下次随访（`next_followup_at`），之后登记死亡或迁出——这一列不清（生命周期事件只改状态）。
`GET /api/spd/patients/{id}/profile` 的卡片原样给这一列，页面 `spdProfileHtml` 也不看状态直接印：开发库实测（修前代码）
卡片「高血压 已死亡 … 下次随访 2026-12-29」，而居民端聚合（`/api/portal/me/enrollments/all`）同一份档案按 P2-973
早已留空——P2-973 的规矩是只给在管档案下次随访，结案、迁出、死亡的不清这一列，原样给会露出过期日期。

修法：接口卡片里非在管档案的 `next_followup_at` 给空串（只改出参，库里那一列不动）；页面只对在管档案印「下次随访」。
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


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21199 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21199 患者", "id_card": "330106194505051199", "gender": "男", "birth_date": "1945-05-05"})
    assert patient.status_code in (200, 201), patient.text
    pid = patient.json()["id"]
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": pid, "program_code": "hypertension", "org_id": org})
    assert enrolled.status_code == 201, enrolled.text
    task = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": pid, "title": "季度随访", "task_type": "followup", "enrollment_id": enrolled.json()["id"],
        "org_id": org})
    assert task.status_code == 201, task.text
    done = client.post(f"{B}/tasks/{task.json()['id']}/complete", headers=admin, json={"result": {}})
    assert done.status_code == 200, done.text
    with SessionLocal() as db:   # 另一病种的档案已迁出，那一列同样没清
        db.add(SpdEnrollment(patient_id=pid, org_id=org, program_code="diabetes", risk_level="mid",
                             stage="管理期", status="migrated", next_followup_at="2026-12-29"))
        db.commit()
    return {"patient": pid, "enrollment": enrolled.json()["id"]}


def _cards(client, admin, pid):
    body = client.get(f"{B}/patients/{pid}/profile", headers=admin)
    assert body.status_code == 200, body.text
    return {g["enrollment"]["program_code"]: (g["enrollment"]["status"], g["enrollment"]["next_followup_at"])
            for g in body.json()["programs"]}


def test_登记死亡后卡片不再给下次随访_库里那一列不动(client, admin, world):
    from app.spd.models import SpdEnrollment

    before = _cards(client, admin, world["patient"])
    status, due = before["hypertension"]
    assert status == "active" and due, before   # 随访办结排上了下次随访，在管时照给
    assert before["diabetes"] == ("migrated", "")   # 修前 ("migrated", "2026-12-29")

    died = client.post(f"{B}/enrollments/{world['enrollment']}/lifecycle", headers=admin,
                       json={"event": "death", "reason": "心源性猝死"})
    assert died.status_code == 200, died.text
    assert _cards(client, admin, world["patient"])["hypertension"] == ("dead", "")   # 修前 ("dead", 原日期)
    with SessionLocal() as db:   # 只改出参：档案那一列照旧，恢复在管时原样回来
        assert db.get(SpdEnrollment, world["enrollment"]).next_followup_at == due


def _function(name: str) -> str:
    start = SRC.index(f"function {name}(")
    return SRC[start:SRC.index("\n}\n", start) + 2]


def _const(name: str) -> str:
    start = SRC.index(f"const {name} = {{")
    return SRC[start:SRC.index("};", start) + 2]


def test_卡片只对在管档案印下次随访():
    card = strip_comments(_function("spdProfileHtml"))
    assert 'const nextFollowup = e.status === "active" ? ` · 下次随访 ${esc(e.next_followup_at || "—")}` : "";' in card
    assert card.count("下次随访") == 1, "卡片里「下次随访」只能出现在在管那一支"   # 修前不看状态直接印


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_跑一遍_已死亡的卡片没有下次随访_在管的照印():
    stubs = ("const esc = (s) => String(s ?? \"\");\nconst spdTag = () => \"\";\nconst table = () => \"\";\n"
             "const panel = (title, body) => body;\nconst SPD_RISK = {}, SPD_TASK_STATUS = {}, SPD_MEAS_LEVEL = {},"
             " SPD_REF_STATUS = {};\n")
    script = (stubs + _const("SPD_ENROLL_STATUS") + "\n" + _function("spdProfileHtml")
              + "\nconsole.log(spdProfileHtml(JSON.parse(process.argv[1])));")

    def render(status):
        profile = {"patient": {}, "programs": [{"program_name": "高血压", "open_tasks": 0, "enrollment": {
            "program_code": "hypertension", "status": status, "stage": "管理期", "next_followup_at": "2026-12-29"}}]}
        return subprocess.run(["node", "-e", script, json.dumps(profile, ensure_ascii=False)],
                              capture_output=True, text=True, check=True, timeout=60).stdout

    assert "下次随访 2026-12-29" in render("active")
    for status in ("dead", "migrated", "excluded", "recalled"):   # 哪怕接口给了日期（旧服务端），页面也不印
        html = render(status)
        assert "下次随访" not in html, (status, html)
        assert "2026-12-29" not in html, (status, html)
