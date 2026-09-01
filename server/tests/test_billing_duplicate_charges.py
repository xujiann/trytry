"""账单明细**允许重复记同一收费项**——把这条现状钉住，别再被当成缺陷去"修"。

`test_stage14_concurrency.py` 的 `LOGICAL_UNIQUE_TABLES` 曾登记过一条
"同账单同收费项逻辑唯一（重复记账即多收费）"，据此 `create_bill_detail` 被列为
待修的静默双写点。**这条判据不成立**，2026-09-01 复核后连同该表一起移出清单：

- 明细本身带 `quantity` 列，一次记多份是用数量表达的；但住院床位费、护理费、
  吸氧这类项目是**按天逐条记同一 item_code**，同一次住院自然出现多条同项目明细；
- 真给 `(admission_id, item_code)` 加唯一约束，第二天的合法计费会被拒——那不是
  修缺陷，是把正常业务打断。

真正该防的是"**同一笔**费用被重复提交"（用户双击、前端重试），那要靠请求级幂等
（客户端提交号 / Idempotency-Key）来分辨"这是同一笔"还是"这是新的一笔"，
表级唯一约束区分不了这两者。属另案，不在本档范围。

本档是**特征化网**：钉住"重复记账合法且金额累加"的当前行为。日后若真要上幂等键，
这些用例会明确地告诉改动者：你正在改变一个被记录在案的业务行为，请连同这份说明
一起更新，而不是以为自己在修一个 bug。
"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def setup(client, admin):
    """机构 + 医师 + 患者 + 一次住院 + 一个按天计费的收费项。"""
    org = client.post(
        "/api/organizations",
        json={"name": "重复计费县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "dup_doc", "password": "pass123456", "role": "doctor",
              "org_id": org["id"], "full_name": "杜医生"},
        headers=admin,
    )
    doctor = login(client, "dup_doc", "pass123456")
    ward = client.post(
        "/api/inpatient/wards",
        json={"org_id": org["id"], "name": "内二科", "ward_type": "general"},
        headers=admin,
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "DUP-1"}, headers=admin
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "重复计费甲", "id_card": "330281198202020018", "gender": "女",
              "birth_date": "1982-02-02"},
        headers=admin,
    ).json()
    admission = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "杜医生"},
        headers=admin,
    ).json()
    created_item = client.post(
        "/api/billing/charge-items",
        json={"code": "BED-DUP", "name": "床位费", "category": "bed", "price": 50},
        headers=admin,
    )
    assert created_item.status_code == 201, created_item.text
    item = created_item.json()
    return {"org": org, "doctor": doctor, "patient": patient,
            "admission": admission, "item": item}


def test_同一收费项按天重复记账合法且金额累加(client, admin, setup):
    """住院床位费天天记一条同 item_code——这正是"同账单同收费项唯一"会打断的业务。"""
    body = {
        "patient_id": setup["patient"]["id"],
        "admission_id": setup["admission"]["id"],
        "item_code": "BED-DUP",
        "quantity": 1,
    }
    created = [client.post("/api/billing/details", json=body, headers=admin)
               for _ in range(3)]
    assert [r.status_code for r in created] == [201, 201, 201], [r.text for r in created]
    assert len({r.json()["id"] for r in created}) == 3, "三次记账应是三条独立明细"

    details = client.get(
        "/api/billing/details",
        params={"admission_id": setup["admission"]["id"]}, headers=admin,
    ).json()
    bed_rows = [d for d in details if d["item_code"] == "BED-DUP"]
    assert len(bed_rows) == 3
    assert sum(d["amount"] for d in bed_rows) == 150, "三天床位费应累加为 150，不是被去重成 50"


def test_数量与逐条记账是两种合法写法(client, admin, setup):
    """quantity=3 与记三条 quantity=1 都合法，金额一致——去重会把后者砍成 50。"""
    body = {
        "patient_id": setup["patient"]["id"],
        "admission_id": setup["admission"]["id"],
        "item_code": "BED-DUP",
        "quantity": 3,
    }
    resp = client.post("/api/billing/details", json=body, headers=admin)
    assert resp.status_code == 201, resp.text
    assert resp.json()["amount"] == 150


def test_账单明细没有被加上唯一约束():
    """防"好心的修复"：谁按旧判据给 (admission_id, item_code) 加了唯一约束，这里变红。"""
    from app.models import Base

    table = Base.metadata.tables["bill_details"]
    unique_keys = [
        [c.name for c in index.columns] for index in table.indexes if index.unique
    ] + [list(c.columns.keys()) for c in table.constraints
         if c.__class__.__name__ == "UniqueConstraint"]
    for key in unique_keys:
        assert "item_code" not in key, (
            f"bill_details 上出现了含 item_code 的唯一约束 {key}——"
            f"按天逐条记同一收费项是正常业务（床位费/护理费），加约束会拒掉第二天的计费。"
            f"若确要防重复提交，请走请求级幂等键，并连同本档 docstring 一起更新"
        )
