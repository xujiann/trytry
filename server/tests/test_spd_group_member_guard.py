"""慢专病患者分组的成员增删守卫（第十二批「批量 vs 单条」扫描 Z4-1 / Z4-5）。

P0-50：批量加成员原先只看分组所属机构能不能写，手工给的患者号一个不判——任一机构的医生按号就能把与本机构毫无关系的
患者拉进自己的分组，成员清单随即回出姓名、健康卡号、电话，不留调阅痕迹；同子系统的批量干预、宣教推送（P0-34）逐个判
可见性并留痕。不存在的患者号原先静默跳过、回执照样 200。
"""
import ast
from pathlib import Path

import pytest

from conftest import login

from app.database import SessionLocal
from app.models import AccessLog

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for tag in ("甲", "乙"):
        orgs[tag] = client.post("/api/organizations", headers=admin, json={
            "name": f"P0050 {tag}卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p0050_doc_b", "password": "pass123456", "role": "doctor", "org_id": orgs["乙"]})
    assert created.status_code == 201, created.text
    doc_b = login(client, "p0050_doc_b", "pass123456")
    patients = {}
    for tag, org in (("甲", orgs["甲"]), ("乙", orgs["乙"])):
        pid = client.post("/api/patients", headers=admin, json={
            "name": f"P0050 {tag}院患者", "id_card": f"33012719600101500{len(patients)}",
            "phone": "13900001111"}).json()["id"]
        # 患者与本院的服务关系：在本院有一次门诊
        enc = client.post("/api/encounters", headers=admin, json={"patient_id": pid, "org_id": org})
        assert enc.status_code == 201, enc.text
        patients[tag] = pid
    group = client.post("/api/spd/groups", headers=doc_b, json={"name": "P0050 乙院分组", "scope": "dept"})
    assert group.status_code == 201, group.text
    return {"doc_b": doc_b, "patients": patients, "group": group.json()["id"]}


def _members(client, headers, group_id):
    return {m["patient_id"] for m in client.get(f"/api/spd/groups/{group_id}/members", headers=headers).json()}


def test_别院患者按号拉不进本院分组(client, admin, world):
    resp = client.post(f"/api/spd/groups/{world['group']}/members", headers=world["doc_b"],
                       json={"patient_ids": [world["patients"]["乙"], world["patients"]["甲"]]})
    assert resp.status_code == 403, resp.text   # 修前 200 {"added": 2}
    # 先全部判完再写：本院那位也没进去
    assert _members(client, admin, world["group"]) == set()


def test_本院患者照常加入并留调阅痕迹(client, admin, world):
    mine = world["patients"]["乙"]
    with SessionLocal() as db:
        before = db.query(AccessLog).filter(AccessLog.patient_id == mine, AccessLog.resource == "spd_group").count()
    resp = client.post(f"/api/spd/groups/{world['group']}/members", headers=world["doc_b"],
                       json={"patient_ids": [mine]})
    assert resp.status_code == 200 and resp.json()["added"] == 1, resp.text
    with SessionLocal() as db:
        after = db.query(AccessLog).filter(AccessLog.patient_id == mine, AccessLog.resource == "spd_group").count()
    assert after == before + 1   # 修前不留痕


def test_不存在的患者号404(client, world):
    resp = client.post(f"/api/spd/groups/{world['group']}/members", headers=world["doc_b"],
                       json={"patient_ids": [99999999]})
    assert resp.status_code == 404 and "99999999" in resp.json()["detail"], resp.text   # 修前 200 {"added": 0}


def _patient_ids_endpoints() -> list[tuple[str, str, bool]]:
    """路由层里请求体带 `patient_ids` 列表的写接口：(文件, 函数, 是否逐个 assert_patient_visible)。"""
    out = []
    for path in sorted((APP / "routers").rglob("*.py")) + sorted((APP / "spd" / "routers").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        models = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and any(isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
                    and item.target.id == "patient_ids" for item in node.body)
        }
        if not models:
            continue
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef):
                continue
            annotated = {ast.unparse(arg.annotation) for arg in func.args.args if arg.annotation is not None}
            if not annotated & models:
                continue
            guarded = any(isinstance(n, ast.Call) and ast.unparse(n.func).endswith("assert_patient_visible")
                          for n in ast.walk(func))
            out.append((path.relative_to(APP).as_posix(), func.name, guarded))
    return out


def test_请求体带患者号列表的写接口逐个判可见性():
    found = _patient_ids_endpoints()
    assert len(found) >= 3, found   # 分组加成员、批量干预、宣教推送——识别不出来说明判据失效
    assert [f"{path}:{name}" for path, name, guarded in found if not guarded] == []
