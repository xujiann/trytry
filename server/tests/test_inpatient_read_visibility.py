"""住院医嘱 / 执行记录 / 病案首页三个读接口的归属校验与留痕（P0-19）。

## 修的是什么

P0-10 收口的是 `clinical_docs` 那一族（病程、护理、体征）；同一份住院病历的另外三块
——**医嘱、医嘱执行记录、病案首页**——在 `inpatient.py` 里，读接口连调用方身份都没收：

    GET /api/inpatient/orders?admission_id=1        → 200  医嘱内容
    GET /api/inpatient/orders                        → 200  全县在用医嘱（不带住院号）
    GET /api/inpatient/orders/1/executions           → 200  执行人、皮试结果
    GET /api/inpatient/admissions/1/case-summary     → 200  出院诊断、转归、费用

（2026-09-24 做 P2-38 住院页时撞见，乙院医生、与该患者毫无业务关系，实测。）
同一个文件里写侧早就收了（`stop_order` 的注释写着"那是认证不是授权"），读侧没跟。

## 为什么三道闸门都没报

- 横向越权闸门的分母是「入参含患者标识」或「`db.get(带 patient_id 的模型, …)`」——
  这三张表都不带 `patient_id`，归属隔一跳在 `admissions` 上；
- 无身份读接口那条（`test_unscopable_patient_reads.py`）只认响应里的**身份字段**
  （patient_id / 姓名 / 证件号……）——这三个响应只有住院号，没有身份字段；
- 分页棘轮只问翻不翻页。

「判据只认一种写法」：病历内容本身不是身份字段，可它是病历。

## 这份用例守什么

两个方向都钉：无关机构 403（带住院号的三条）/ 看不到（不带住院号的清单），有业务关系的
照常 200 并留下 `AccessLog`（否则"修好了"可能只是"全关了"）。
"""
import pytest

from app.database import SessionLocal
from app.models import AccessLog


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def inpatient_world(client):
    """甲乙两院各一名医师；患者只在甲院有就诊记录并在甲院住院，甲院医师开了医嘱、登记了执行、填了病案首页。"""
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "住院读甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "住院读乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org in (("ipr_doc_a", a), ("ipr_doc_b", b)):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": "doctor", "org_id": org["id"]},
                    headers=admin)
    doc_a, doc_b = _login(client, "ipr_doc_a"), _login(client, "ipr_doc_b")
    patient = client.post("/api/patients",
                          json={"name": "住院读患者", "id_card": "320000199202025678"},
                          headers=admin).json()
    client.post("/api/encounters",
                json={"patient_id": patient["id"], "org_id": a["id"], "encounter_type": "inpatient"},
                headers=doc_a)
    ward = client.post("/api/inpatient/wards",
                       json={"name": "住院读病区", "org_id": a["id"]}, headers=admin).json()
    bed = client.post("/api/inpatient/beds",
                      json={"ward_id": ward["id"], "bed_no": "R-01"}, headers=admin).json()
    adm = client.post("/api/inpatient/admissions",
                      json={"patient_id": patient["id"], "org_id": a["id"],
                            "ward_id": ward["id"], "bed_id": bed["id"],
                            "doctor_name": "甲医生", "diagnosis_name": "肺炎"},
                      headers=doc_a)
    assert adm.status_code == 201, adm.text
    adm_id = adm.json()["id"]
    order = client.post("/api/inpatient/orders",
                        json={"admission_id": adm_id, "order_type": "temp", "content": "头孢曲松 2g ivgtt st"},
                        headers=doc_a)
    assert order.status_code == 201, order.text
    order_id = order.json()["id"]
    ex = client.post(f"/api/inpatient/orders/{order_id}/executions",
                     json={"note": "已执行", "skin_test_result": "negative"}, headers=doc_a)
    assert ex.status_code == 201, ex.text
    cs = client.post(f"/api/inpatient/admissions/{adm_id}/case-summary",
                     json={"discharge_diagnosis": "社区获得性肺炎", "outcome": "治愈"}, headers=doc_a)
    assert cs.status_code == 201, cs.text
    return {"doc_a": doc_a, "doc_b": doc_b, "patient_id": patient["id"],
            "admission_id": adm_id, "order_id": order_id}


def _reads(w):
    return {
        "orders": f"/api/inpatient/orders?admission_id={w['admission_id']}",
        "executions": f"/api/inpatient/orders/{w['order_id']}/executions",
        "case_summary": f"/api/inpatient/admissions/{w['admission_id']}/case-summary",
    }


@pytest.mark.parametrize("name", ["orders", "executions", "case_summary"])
def test_无关机构按住院号读医嘱_执行_病案首页一律403(client, inpatient_world, name):
    r = client.get(_reads(inpatient_world)[name], headers=inpatient_world["doc_b"])
    assert r.status_code == 403, (name, r.status_code, r.text)


def test_不带住院号的医嘱清单只列可见患者的(client, inpatient_world):
    """不带 `admission_id` 时原先吐全县在用医嘱；现在与住院清单同口径（`scope_patient_list`）。"""
    mine = client.get("/api/inpatient/orders", headers=inpatient_world["doc_a"])
    theirs = client.get("/api/inpatient/orders", headers=inpatient_world["doc_b"])
    assert mine.status_code == theirs.status_code == 200
    assert inpatient_world["order_id"] in {o["id"] for o in mine.json()}
    assert inpatient_world["order_id"] not in {o["id"] for o in theirs.json()}


def test_不存在的住院号照旧回空清单(client, inpatient_world):
    """清单的过滤条件匹配不到就是空——不因为补了归属校验就变成 404（响应形状不变）。"""
    r = client.get("/api/inpatient/orders?admission_id=987654", headers=inpatient_world["doc_b"])
    assert r.status_code == 200 and r.json() == []


@pytest.mark.parametrize(
    "name,resource",
    [("orders", "inpatient_order"), ("executions", "inpatient_order"), ("case_summary", "case_summary")],
)
def test_有业务关系的照常能读且留痕(client, inpatient_world, name, resource):
    def count():
        db = SessionLocal()
        try:
            return db.query(AccessLog).filter(
                AccessLog.patient_id == inpatient_world["patient_id"], AccessLog.resource == resource
            ).count()
        finally:
            db.close()

    before = count()
    r = client.get(_reads(inpatient_world)[name], headers=inpatient_world["doc_a"])
    assert r.status_code == 200, (name, r.text)
    assert r.json(), name  # 真读到了东西，不是空壳
    assert count() == before + 1, f"{name} 放行了却没留痕"
