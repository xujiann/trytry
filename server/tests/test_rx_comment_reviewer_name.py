"""处方点评清单带出点评人（P2-1666，第四十九批「集中审方与药事监测」扫描 AM3-9 的出参一半）。

`PrescriptionComment.reviewer_id` 落了库，`RxCommentOut` 却不出：点评表只有「处方ID / 结论 / 问题类型 / 点评意见 / 时间」，
看不出是谁评的——而兄弟路径规则改动记录 `DrugRuleChangeOut.changed_by` 是出姓名的。实测（修前）行的键为
`['at','comment','grade','id','issues','prescription_id']`。

修法：出参**末尾**追加 `commented_by`（点评人姓名，写法同 `changed_by`：没填姓名的退回账号、取不到的为空串；按页一次取齐，
不逐行查），集中审方页点评表加「点评人」一列。点评能不能更正另行登记，不在本条。
"""
from pathlib import Path

import pytest

from conftest import login

CORE = (Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8")
OLD_KEYS = ["id", "prescription_id", "grade", "issues", "comment", "at"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1666 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    for username, full_name in (("p1666_ph1", "P1666 李药师"), ("p1666_ph2", "")):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": full_name, "role": "pharmacist", "org_id": org})
        assert resp.status_code in (200, 201), resp.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1666 患者", "id_card": "330102195001011666"}).json()["id"]
    out = {}
    for username in ("p1666_ph1", "p1666_ph2"):
        rx = client.post("/api/prescriptions", headers=admin, json={
            "patient_id": patient, "org_id": org, "diagnosis_name": "感冒",
            "items": [{"drug_code": "P1666-AMB", "drug_name": "P1666 氨溴索", "daily_dose": 60, "days": 3}]})
        assert rx.status_code == 201, rx.text
        made = client.post(f"/api/prescriptions/{rx.json()['id']}/comment-review",
                           headers=login(client, username, "passw0rd1"), json={"grade": "reasonable"})
        assert made.status_code == 201, made.text
        out[username] = made.json()["id"]
    return out


def test_点评清单末尾带点评人姓名_原有键与次序不动(client, admin, world):
    rows = {r["id"]: r for r in client.get("/api/prescriptions/comment-reviews", headers=admin).json()}
    for comment_id in world.values():
        assert list(rows[comment_id]) == OLD_KEYS + ["commented_by"]   # 修前没有这个键
    assert rows[world["p1666_ph1"]]["commented_by"] == "P1666 李药师"
    assert rows[world["p1666_ph2"]]["commented_by"] == "p1666_ph2"   # 没填姓名的退回账号（同 changed_by）


def test_点评表有点评人一列():
    start = CORE.index('panel("处方点评（事后监管）"')
    block = CORE[start:CORE.index("</tr>`)}`)}", start)]
    assert '"点评人"' in block and "esc(c.commented_by)" in block, block   # 修前表头只有处方ID / 结论 / 问题类型 / 点评意见 / 时间
