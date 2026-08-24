"""慢专病配置域 · 病种目录与专病中心的**响应契约**（catalog 9 + centers 4）。

`spd/config` 是个包（ADR-0008 拆的），58 个端点分在 6 个子模块里。按子模块分批
治理——一次比 58 个端点，逐字节比对出了问题不好定位，粒度本身就是这套办法的价值。

本批的三处判断：

1. **`ProgramDetailOut` 继承 `ProgramOut` 是对的**：`get_program` 在
   `_program_out` 的结果上只追加 `targets`，是严格超集。（`spd/portal` 那批的
   转诊详情不是超集，同样写继承就错了，被响应校验当场拦下——两种情况要分清。）
2. **`target_low`/`target_high` 是可空 Float**：定性目标（戒烟、规律服药）没有
   上下限，为 null；量化目标的整数下限读回来是 `90.0`。
3. **`org-tree` 是自引用递归模型**：树深由数据决定（县—乡—村三级，市级四层也
   合法），摊平成固定层级的字段就写死了层数。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import Organization, User
from app.security import hash_password
from app.spd import models as S


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seeded(client):
    with SessionLocal() as db:
        county = Organization(name="配置县医院", org_type="hospital", level="county")
        db.add(county)
        db.flush()
        town = Organization(name="配置卫生院", org_type="clinic", level="town",
                            parent_id=county.id)
        db.add(town)
        db.flush()
        village = Organization(name="配置卫生室", org_type="station", level="village",
                               parent_id=town.id)
        db.add(village)
        db.flush()
        admin = User(username="cfgcontract", password_hash=hash_password("Cfg-ct-2026!"),
                     full_name="配置管理员", role="admin", org_id=county.id)
        db.add(admin)
        db.flush()
        prog = S.SpdProgram(code="CT-HTN", name="契约高血压", category="chronic",
                            lead_org_id=county.id, lead_dept="心内科", description="说明",
                            include_rules=[], exclude_rules=[],
                            stages=[{"key": "stable", "name": "稳定期"}], milestones=[],
                            version="v1", effective_from="2026-01-01", active=True)
        db.add(prog)
        db.flush()
        # 量化目标：整数下限 + 小数上限；定性目标：上下限均为 null
        db.add(S.SpdTarget(program_id=prog.id, stage="stable", metric="bp_sys",
                           metric_name="收缩压", kind="quantitative", target_low=90,
                           target_high=139.5, unit="mmHg", followup_interval_days=90))
        db.add(S.SpdTarget(program_id=prog.id, stage="stable", metric="smoke",
                           metric_name="吸烟", kind="qualitative", qualitative="已戒烟",
                           risk_level="mid", followup_interval_days=180))
        # 老快照：字段比现在少，逐字段建模会给它注入 null
        db.add(S.SpdProgramVersion(program_id=prog.id, version="v0",
                                   snapshot={"code": "CT-HTN", "version": "v0"},
                                   changed_by="cfgcontract", note="初始"))
        team = S.SpdTeam(name="配置团队", org_id=town.id, active=True)
        db.add(team)
        db.flush()
        db.add(S.SpdEnrollment(patient_id=1, program_code="CT-HTN", org_id=town.id,
                               status="active"))
        center = S.SpdCenter(code="CT-CTR", name="契约中心", program_code="CT-HTN",
                             lead_org_id=county.id, lead_dept="心内科",
                             leader_user_id=admin.id, org_ids=[county.id, town.id],
                             team_ids=[team.id], version="v1", status="active")
        db.add(center)
        db.commit()
        return {"prog": prog.id, "center": center.id, "county": county.id,
                "town": town.id, "village": village.id, "team": team.id,
                "target": db.query(S.SpdTarget).order_by(S.SpdTarget.id).first().id}


@pytest.fixture(scope="module")
def auth(client, seeded):
    token = client.post("/api/auth/login",
                        json={"username": "cfgcontract",
                              "password": "Cfg-ct-2026!"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


B = "/api/spd"

PROGRAM_KEYS = {"id", "code", "name", "category", "lead_org_id", "lead_dept",
                "description", "include_rules", "exclude_rules", "stages", "milestones",
                "version", "effective_from", "active"}
TARGET_KEYS = {"id", "program_id", "stage", "metric", "metric_name", "kind", "target_low",
               "target_high", "unit", "qualitative", "risk_level",
               "followup_interval_days", "form_code", "edu_code", "active"}


# ------------------------------------------------- 详情是列表的严格超集
def test_专病详情比列表只多出targets(client, auth, seeded):
    """继承 `ProgramOut` 的前提就是这句话成立。它一旦不成立（详情少了某个字段），
    继承就会凭空要求一个不返回的键——那正是 spd/portal 转诊详情踩的坑。"""
    listed = {p["code"]: p for p in client.get(f"{B}/programs", headers=auth).json()}
    assert set(listed["CT-HTN"]) == PROGRAM_KEYS
    detail = client.get(f"{B}/programs/{seeded['prog']}", headers=auth).json()
    assert set(detail) - set(listed["CT-HTN"]) == {"targets"}
    assert set(listed["CT-HTN"]) - set(detail) == set()
    # 除 targets 外逐字段相等
    assert {k: v for k, v in detail.items() if k != "targets"} == listed["CT-HTN"]


# ------------------------------------------------- 可空 Float
def test_定性目标的上下限是null量化目标的整数下限是float(client, auth, seeded):
    rows = {t["metric"]: t for t in
            client.get(f"{B}/programs/{seeded['prog']}/targets", headers=auth).json()}
    assert set(rows["bp_sys"]) == TARGET_KEYS
    assert rows["bp_sys"]["target_low"] == 90.0
    assert isinstance(rows["bp_sys"]["target_low"], float)
    assert rows["bp_sys"]["target_high"] == 139.5
    # 定性目标没有上下限——不是 0，是 null
    assert rows["smoke"]["target_low"] is None and rows["smoke"]["target_high"] is None


# ------------------------------------------------- 递归树
def test_机构树是三级递归且带团队数与在管数(client, auth, seeded):
    roots = client.get(f"{B}/org-tree", headers=auth).json()
    node_keys = {"id", "name", "org_type", "level", "parent_id", "team_count",
                 "enrolled", "children"}
    county = next(n for n in roots if n["id"] == seeded["county"])
    assert set(county) == node_keys
    assert county["parent_id"] is None          # 根节点无上级：null，不是 0
    town = next(c for c in county["children"] if c["id"] == seeded["town"])
    assert set(town) == node_keys and town["parent_id"] == seeded["county"]
    assert town["team_count"] == 1 and town["enrolled"] == 1
    village = next(c for c in town["children"] if c["id"] == seeded["village"])
    # 第三层仍是同一形状，且叶子的 children 是空列表而不是缺失
    assert set(village) == node_keys and village["children"] == []


# ------------------------------------------------- 快照宽字典
def test_历史快照按原样透出不被现在的形状改写(client, auth, seeded):
    """快照的意义就是"当时长什么样"。逐字段建模会给字段更少的老快照注入 null，
    等于用今天的形状改写历史。"""
    rows = client.get(f"{B}/programs/{seeded['prog']}/versions", headers=auth).json()
    assert set(rows[0]) == {"id", "version", "changed_by", "note", "snapshot",
                            "created_at"}
    old = next(r for r in rows if r["version"] == "v0")
    assert old["snapshot"] == {"code": "CT-HTN", "version": "v0"}


# ------------------------------------------------- 元数据与中心
def test_规则元数据的四组选项(client, auth):
    body = client.get(f"{B}/meta", headers=auth).json()
    assert set(body) == {"fields", "operators", "risk_levels", "task_types",
                         "member_roles"}
    assert all(set(f) == {"key", "name"} for f in body["fields"])
    assert all(set(f) == {"key", "name"} for f in body["operators"])
    # 风险等级多一个 color（前端拿它上色），故是 RuleOptionOut 的子类
    assert all(set(r) == {"key", "name", "color"} for r in body["risk_levels"])
    assert body["task_types"]["followup"] == "随访"
    assert body["member_roles"]["village_doctor"] == "村医"


def test_专病中心的键集合与id数组(client, auth, seeded):
    rows = client.get(f"{B}/centers", headers=auth).json()
    center = next(c for c in rows if c["code"] == "CT-CTR")
    assert set(center) == {"id", "code", "name", "program_code", "lead_org_id",
                           "lead_dept", "leader_user_id", "org_ids", "team_ids",
                           "version", "status"}
    assert center["org_ids"] == [seeded["county"], seeded["town"]]
    assert center["team_ids"] == [seeded["team"]]
    filtered = client.get(f"{B}/centers", headers=auth,
                          params={"program_code": "CT-HTN"}).json()
    assert [c["code"] for c in filtered] == ["CT-CTR"]


# ------------------------------------------------- 写侧与错误体
def test_写侧端点回同一形状(client, auth, seeded):
    created = client.post(f"{B}/programs", headers=auth,
                          json={"code": "CT-DM", "name": "契约糖尿病",
                                "category": "specialty", "lead_dept": "内分泌"})
    assert created.status_code == 201 and set(created.json()) == PROGRAM_KEYS
    # 未指定牵头机构 → null，不是 0
    assert created.json()["lead_org_id"] is None

    patched = client.patch(f"{B}/programs/{seeded['prog']}", headers=auth,
                           json={"description": "改了说明"})
    assert patched.status_code == 200 and set(patched.json()) == PROGRAM_KEYS
    assert patched.json()["description"] == "改了说明"

    target = client.post(f"{B}/programs/{seeded['prog']}/targets", headers=auth,
                         json={"stage": "risky", "metric": "bp_dia", "kind": "quantitative",
                               "target_low": 60, "target_high": 89, "unit": "mmHg"})
    assert target.status_code == 201 and set(target.json()) == TARGET_KEYS

    tp = client.patch(f"{B}/targets/{seeded['target']}", headers=auth,
                      json={"unit": "kPa"})
    assert tp.status_code == 200 and set(tp.json()) == TARGET_KEYS and tp.json()["unit"] == "kPa"

    center = client.post(f"{B}/centers", headers=auth,
                         json={"code": "CT-CTR2", "name": "第二中心",
                               "program_code": "CT-HTN"})
    assert center.status_code == 201
    assert center.json()["leader_user_id"] is None and center.json()["org_ids"] == []

    cp = client.patch(f"{B}/centers/{seeded['center']}", headers=auth,
                      json={"status": "paused"})
    assert cp.status_code == 200 and cp.json()["status"] == "paused"


def test_各类错误体都只有detail(client, auth, seeded):
    cases = [
        client.get(f"{B}/programs/999999", headers=auth),
        client.post(f"{B}/programs", headers=auth,
                    json={"code": "CT-HTN", "name": "重复编码"}),
        client.post(f"{B}/programs/{seeded['prog']}/targets", headers=auth,
                    json={"stage": "x", "metric": "bmi", "kind": "quantitative"}),
        client.patch(f"{B}/targets/999999", headers=auth, json={"unit": "x"}),
        client.post(f"{B}/centers", headers=auth,
                    json={"code": "CT-NONE", "name": "无此病种", "program_code": "NOPE"}),
        client.patch(f"{B}/centers/999999", headers=auth, json={"name": "x"}),
    ]
    assert [r.status_code for r in cases] == [404, 409, 422, 404, 404, 404]
    for r in cases:
        assert set(r.json()) == {"detail"}
