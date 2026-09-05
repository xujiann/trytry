"""一本孕产妇档案只登记一次分娩（P1-30 · `delivery_records`）。

洞的形状与 P1-29 的三条同源：`add_delivery` 先查"这本册子有没有分娩记录"、
没有就建，两步之间没有闸门。并发下两路都查不到，就都建——**不报错，静默写出
两条**；而查询侧 `get_delivery` 是无 `order_by` 的 `.first()`，此后取到哪条全看
运气：分娩方式、新生儿数可能各不相同，产科事后只能从两条里靠病历判哪条算数。
多胎不是多行（由 `newborn_count` 1..5 表达），所以这条唯一性是全量的，不带状态条件。

`uq_delivery_record`（模型 + 迁移 `b9c8d7e6f5a4`）把它下沉到库，接口层改走
`insert_or_conflict`。本档钉三件事：

1. **行为面**：抢输的一路拿到的 409 与顺序重复请求逐字相同（对调用方而言
   "并发撞车"与"本来就重复"没有区别）；不同档案各自登记一次仍是 201——
   键写宽了（比如误按机构或日期唯一）这条会红。
2. **防拆卸静态钉**：索引必须留在模型上、`unique=True`、键恰为 `record_id`，
   且**不是**部分索引；同时按真实表结构再钉一遍（模型声明了而库里没建 = 没约束）。
3. **绕开接口层直插**：顺序请求被预检拦在库门之前，行为用例因此分辨不出兜底是否
   真的生效，而 SQLite 的库级写锁又让线程探针对"拆掉索引"不敏感——直接写库
   （那正是并发抢输者实际到达的位置）才是确定性的网。真并发下的"恰一个赢家"
   由 `tests/test_maternal_delivery_unique_races.py` 在真 PG 上验证。
"""
import pytest
from sqlalchemy import inspect as sa_inspect

from conftest import login

from app.database import engine
from app.models import Base

DUPLICATE_DETAIL = "该档案已有分娩记录"


@pytest.fixture(scope="module")
def setup(client, admin):
    """一家机构 + 一名本机构医师（分娩登记限 doctor 角色，且要能以该机构名义写入）。"""
    org = client.post(
        "/api/organizations",
        json={"name": "分娩唯一县妇幼保健院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "md_doc", "password": "pass123456", "role": "doctor",
              "org_id": org["id"], "full_name": "毛医生"},
        headers=admin,
    )
    return {"org": org, "doctor": login(client, "md_doc", "pass123456")}


def _record(client, headers, name, id_card):
    """建一本孕产妇册子（每条用例各用各的患者：一个患者只有一本册子）。"""
    patient = client.post(
        "/api/patients",
        json={"name": name, "id_card": id_card, "gender": "女", "birth_date": "1995-02-02"},
        headers=headers,
    ).json()
    record = client.post(
        "/api/maternal/records",
        json={"patient_id": patient["id"], "lmp": "2026-01-01", "edc": "2026-10-08"},
        headers=headers,
    )
    assert record.status_code in (200, 201), record.text
    return record.json()


# ================================================================ 行为面


def test_同一档案第二次分娩登记409且库里仍只有一条(client, setup):
    doc = setup["doctor"]
    record = _record(client, doc, "分娩唯一甲", "330281199502020017")
    body = {"org_id": setup["org"]["id"], "delivery_date": "2026-10-05",
            "delivery_mode": "cesarean", "outcome": "母子平安"}

    first = client.post(f"/api/maternal/records/{record['id']}/delivery", json=body, headers=doc)
    assert first.status_code == 201, first.text
    assert first.json() == {
        "id": first.json()["id"], "record_id": record["id"],
        "delivery_mode": "cesarean", "status": "delivered",
    }

    again = client.post(
        f"/api/maternal/records/{record['id']}/delivery",
        json=dict(body, delivery_date="2026-10-06", delivery_mode="natural"),
        headers=doc,
    )
    assert again.status_code == 409
    assert again.json()["detail"] == DUPLICATE_DETAIL

    # 第二条没写进去：查询侧无序 `.first()`，写出两条之后取到哪条全看运气
    detail = client.get(f"/api/maternal/records/{record['id']}/delivery", headers=doc)
    assert detail.status_code == 200, detail.text
    assert detail.json()["delivery_mode"] == "cesarean"
    assert detail.json()["delivery_date"] == "2026-10-05"


def test_不同档案各自登记一次互不冲突(client, setup):
    """唯一性只按"一本册子"划界：键写宽了（按机构/按日期）这条会红。"""
    doc = setup["doctor"]
    body = {"org_id": setup["org"]["id"], "delivery_date": "2026-10-05"}
    for name, id_card in (("分娩唯一乙", "330281199502020033"), ("分娩唯一丙", "330281199502020050")):
        record = _record(client, doc, name, id_card)
        resp = client.post(
            f"/api/maternal/records/{record['id']}/delivery", json=body, headers=doc
        )
        assert resp.status_code == 201, f"{name} 同机构同日分娩不该冲突：{resp.text}"


def test_结案档案登记分娩仍是原来那句409(client, setup):
    """另一条 409 是别的判据（档案已结案），不该被本次改动串味。"""
    doc = setup["doctor"]
    record = _record(client, doc, "分娩唯一丁", "330281199502020076")
    # 结案要求 status=delivered：产后访视把册子推到 delivered，再结案
    assert client.post(
        f"/api/maternal/records/{record['id']}/visits",
        json={"visit_type": "postpartum", "note": "产后访视", "visit_date": "2026-11-01"},
        headers=doc,
    ).status_code == 201
    assert client.post(f"/api/maternal/records/{record['id']}/close", headers=doc).status_code == 200

    resp = client.post(
        f"/api/maternal/records/{record['id']}/delivery",
        json={"org_id": setup["org"]["id"], "delivery_date": "2026-10-05"},
        headers=doc,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "档案已结案，不可登记分娩"


# ================================================================ 防拆卸静态钉


def test_分娩唯一索引不许从模型上消失():
    """模型侧的声明就是这条不变式的落点，删掉就等于把静默双写的洞放回去。

    同时钉住"**不是**部分索引"：分娩登记在任何档案状态下都 409（结案态更是先被
    另一句拦掉），多胎由 `newborn_count` 表达，没有"这一态才唯一"的说法——
    加上 WHERE 会在别的状态下重新放开双写。
    """
    index = next(
        (i for i in Base.metadata.tables["delivery_records"].indexes
         if i.name == "uq_delivery_record"),
        None,
    )
    assert index is not None, "delivery_records 的 uq_delivery_record 没了——静默双写的洞回来了"
    assert index.unique, "uq_delivery_record 不再是唯一索引，等于没有约束"
    assert [c.name for c in index.columns] == ["record_id"], "uq_delivery_record 的键变了"
    assert index.dialect_options["sqlite"].get("where") is None, (
        "uq_delivery_record 被改成了部分索引：一档一分娩是全量唯一，"
        "带上条件等于在其余状态下放开双写"
    )


def test_分娩唯一索引真的建在库上():
    """模型声明了、库里没建过（漏迁移）同样等于没有约束——按真实表结构再钉一遍。"""
    names = {i["name"] for i in sa_inspect(engine).get_indexes("delivery_records")}
    assert "uq_delivery_record" in names, "delivery_records 上没有 uq_delivery_record（库与模型对不上）"


def test_绕开接口层直插时库里真的拦得住(client, setup):
    """索引"在不在"与"拦不拦得住"是两回事。

    接口层的预检在顺序请求下就会给出 409，行为用例因此**分辨不出**兜底是否真的
    生效（SQLite 的库级写锁又让线程探针对拆卸不敏感）。这里绕开接口层直接写库
    ——那正是并发抢输者实际到达的位置——看数据库自己是否抬手。
    """
    from sqlalchemy.exc import IntegrityError

    from app.database import SessionLocal
    from app.models import DeliveryRecord

    doc = setup["doctor"]
    record = _record(client, doc, "直插验证戊", "330281199502020092")
    created = client.post(
        f"/api/maternal/records/{record['id']}/delivery",
        json={"org_id": setup["org"]["id"], "delivery_date": "2026-10-05"},
        headers=doc,
    )
    assert created.status_code == 201, created.text

    db = SessionLocal()
    try:
        db.add(DeliveryRecord(
            record_id=record["id"], org_id=setup["org"]["id"],
            delivery_date="2026-10-06", delivery_mode="natural", newborn_count=1, outcome="",
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.query(DeliveryRecord).filter(
            DeliveryRecord.record_id == record["id"]
        ).count() == 1
    finally:
        db.close()
