"""三条"业务上唯一、库上无约束"的不变式下沉到库之后的回归（P1-29）。

洞的形状：接口层都是"先查有没有、没有就建"，两步之间没有闸门。并发下两路都
查不到，就都建——**不报错，静默写出两条**：同一个患者被登记进两张床、同一时段
放出两份号、同一次住院两份首次病程（法定文书）。这类缺陷比撞 `IntegrityError`
更坏：撞了至少有人知道，静默双写要等到有人对账、查房或质控时才发现，而那时
两条记录各自已经挂上了医嘱、费用或预约。

迁移 `b8e3d5f70a91` 把三条不变式下沉为**部分唯一索引**，接口层改走
`insert_or_conflict`。本档钉两件事：

1. **行为面**：抢输的一路拿到 409、且文案与顺序请求完全一致（对调用方来说
   "并发撞车"与"本来就重复"没有区别）；合法的多条不受影响——出院后可再入院、
   日常病程可多条、不同时段号源互不冲突。
2. **防拆卸**：三条索引必须留在模型上且带 `unique=True` 与部分条件。SQLite 的
   库级写锁让线程探针对"拆掉索引"不敏感（拆了照样大概率不重复），静态钉才是
   确定性的网——与 `test_spd_task_claim_race.py` 同一分工。
"""
import pytest
from sqlalchemy import inspect as sa_inspect

from conftest import login

from app.database import engine
from app.models import Base


@pytest.fixture(scope="module")
def ward(client, admin):
    """一个机构 + 病区 + 三张床：每条用例占各自的床，互不串味
    （床位是被原子占用的，共用一张床会让后跑的用例拿到"床位已占"而不是它要测的东西）。"""
    org = client.post(
        "/api/organizations",
        json={"name": "逻辑唯一县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    ward = client.post(
        "/api/inpatient/wards",
        json={"org_id": org["id"], "name": "内一科", "ward_type": "general"},
        headers=admin,
    ).json()
    beds = [
        client.post(
            "/api/inpatient/beds",
            json={"ward_id": ward["id"], "bed_no": f"LU-{i}"},
            headers=admin,
        ).json()
        for i in (1, 2, 3)
    ]
    return {"org": org, "ward": ward, "beds": beds}


@pytest.fixture(scope="module")
def doctor(client, admin, ward):
    """出院端点限 doctor 角色（既有业务规则），单开一个本机构医师账号。"""
    client.post(
        "/api/users",
        json={"username": "lu_doc", "password": "pass123456", "role": "doctor",
              "org_id": ward["org"]["id"], "full_name": "陆医生"},
        headers=admin,
    )
    return login(client, "lu_doc", "pass123456")


def _patient(client, admin, name, id_card):
    return client.post(
        "/api/patients",
        json={"name": name, "id_card": id_card, "gender": "男", "birth_date": "1980-01-01"},
        headers=admin,
    ).json()


# ================================================================ 在院唯一


def test_同患者第二次入院登记409且不产生第二条在院记录(client, admin, ward):
    patient = _patient(client, admin, "在院唯一甲", "330281198001010011")
    body = {
        "patient_id": patient["id"], "ward_id": ward["ward"]["id"],
        "bed_id": ward["beds"][0]["id"], "doctor_name": "张医生",
    }
    first = client.post("/api/inpatient/admissions", json=body, headers=admin)
    assert first.status_code == 201, first.text

    again = dict(body, bed_id=ward["beds"][1]["id"])
    resp = client.post("/api/inpatient/admissions", json=again, headers=admin)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "该患者已在院，不可重复入院登记"

    rows = client.get(
        "/api/inpatient/admissions", params={"patient_id": patient["id"]}, headers=admin
    ).json()
    in_hospital = [r for r in rows if r["status"] == "admitted"]
    assert len(in_hospital) == 1, "静默写出两条在院记录正是本次要防的事"


def test_出院后可再次入院(client, admin, ward, doctor):
    """唯一性只约束"在院"这一态：部分索引写错成全量唯一，这条会红。"""
    patient = _patient(client, admin, "在院唯一乙", "330281198001010038")
    body = {
        "patient_id": patient["id"], "ward_id": ward["ward"]["id"],
        "bed_id": ward["beds"][2]["id"], "doctor_name": "张医生",
    }
    created = client.post("/api/inpatient/admissions", json=body, headers=admin)
    assert created.status_code == 201, created.text
    first = created.json()
    # 出院要先有病案首页，且限医师角色（既有业务规则，不是本次改动）
    summary = client.post(
        f"/api/inpatient/admissions/{first['id']}/case-summary",
        json={"discharge_diagnosis": "社区获得性肺炎", "outcome": "治愈"},
        headers=doctor,
    )
    assert summary.status_code == 201, summary.text
    discharged = client.post(
        f"/api/inpatient/admissions/{first['id']}/discharge", headers=doctor
    )
    assert discharged.status_code == 200, discharged.text
    again = client.post("/api/inpatient/admissions", json=body, headers=admin)
    assert again.status_code == 201, again.text


# ================================================================ 号源唯一


def test_同时段号源重复创建409(client, admin, ward):
    slot = {
        "org_id": ward["org"]["id"], "resource_type": "exam",
        "resource_name": "CT 室", "slot_date": "2026-09-10",
        "slot_time": "09:00", "capacity": 5,
    }
    assert client.post("/api/appointments/slots", json=slot, headers=admin).status_code == 201
    resp = client.post("/api/appointments/slots", json=slot, headers=admin)
    assert resp.status_code == 409
    assert "号源已存在" in resp.json()["detail"]
    # 不挂医师的号源（employee_id 为 NULL）也要被守住：SQL 里 NULL != NULL，
    # 单一复合唯一索引对这类号源等于不设防，故拆了两条部分索引。
    listed = client.get(
        "/api/appointments/slots",
        params={"org_id": ward["org"]["id"], "date_from": "2026-09-10", "date_to": "2026-09-10"},
        headers=admin,
    ).json()
    assert len([s for s in listed if s["resource_name"] == "CT 室"]) == 1


def test_不同时段与不同资源互不冲突(client, admin, ward):
    base = {
        "org_id": ward["org"]["id"], "resource_type": "exam",
        "resource_name": "CT 室", "slot_date": "2026-09-11", "slot_time": "09:00",
    }
    assert client.post("/api/appointments/slots", json=base, headers=admin).status_code == 201
    for changed in ({"slot_time": "10:00"}, {"resource_name": "MR 室"},
                    {"slot_date": "2026-09-12"}):
        resp = client.post("/api/appointments/slots", json=dict(base, **changed), headers=admin)
        assert resp.status_code == 201, f"{changed} 不该冲突：{resp.text}"


# ================================================================ 首次病程唯一


@pytest.fixture(scope="module")
def admission_for_notes(client, admin, ward):
    patient = _patient(client, admin, "病程唯一丙", "330281198001010054")
    return client.post(
        "/api/inpatient/admissions",
        json={
            "patient_id": patient["id"], "ward_id": ward["ward"]["id"],
            "bed_id": ward["beds"][1]["id"], "doctor_name": "李医生",
        },
        headers=admin,
    ).json()


def test_首次病程第二次书写409(client, admin, admission_for_notes):
    aid = admission_for_notes["id"]
    body = {"note_type": "first", "content": "首次病程内容", "recorded_at": "2026-09-10 09:00"}
    assert client.post(
        f"/api/inpatient/admissions/{aid}/progress-notes", json=body, headers=admin
    ).status_code == 201
    resp = client.post(
        f"/api/inpatient/admissions/{aid}/progress-notes", json=body, headers=admin
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "首次病程记录已存在"


def test_日常病程可多条且同一时刻不冲突(client, admin, admission_for_notes):
    """病程是"连续文书流"：把唯一性写成"同住院同时刻唯一"会拒掉正常书写。"""
    aid = admission_for_notes["id"]
    for content in ("查房记录一", "查房记录二"):
        resp = client.post(
            f"/api/inpatient/admissions/{aid}/progress-notes",
            json={"note_type": "daily", "content": content, "recorded_at": "2026-09-10 15:00"},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text


# ================================================================ 防拆卸静态钉


@pytest.mark.parametrize(
    "table,index_name,columns,where_fragment",
    [
        ("admissions", "uq_admission_patient_admitted", ["patient_id"], "admitted"),
        ("appointment_slots", "uq_slot_with_employee",
         ["org_id", "employee_id", "resource_type", "resource_name", "slot_date", "slot_time"],
         "IS NOT NULL"),
        ("appointment_slots", "uq_slot_without_employee",
         ["org_id", "resource_type", "resource_name", "slot_date", "slot_time"], "IS NULL"),
        ("progress_notes", "uq_progress_note_first", ["admission_id"], "first"),
    ],
)
def test_三条不变式的部分唯一索引不许消失(table, index_name, columns, where_fragment):
    """模型侧的声明就是这三条不变式的落点，删掉就等于把洞放回去。

    同时钉住"是部分索引"——写成全量唯一会拒掉合法的多条（出院后再入院、
    日常病程多条），那是另一种坏。
    """
    index = next(
        (i for i in Base.metadata.tables[table].indexes if i.name == index_name), None
    )
    assert index is not None, f"{table} 的 {index_name} 没了——静默双写的洞回来了"
    assert index.unique, f"{index_name} 不再是唯一索引，等于没有约束"
    assert [c.name for c in index.columns] == columns, f"{index_name} 的键变了"
    where = str(index.dialect_options["sqlite"].get("where", ""))
    assert where_fragment in where, (
        f"{index_name} 的部分条件不再包含 {where_fragment!r}：全量唯一会拒掉合法的多条"
    )


def test_绕开接口层直插时库里真的拦得住(client, admin, ward):
    """索引"在不在"与"拦不拦得住"是两回事。

    接口层的预检在顺序请求下就会给出 409，行为用例因此**分辨不出**兜底是否真的
    生效（SQLite 的库级写锁又让线程探针对拆卸不敏感）。这里绕开接口层直接写库
    ——那正是并发抢输者实际到达的位置——看数据库自己是否抬手。
    """
    from sqlalchemy.exc import IntegrityError

    from app.database import SessionLocal
    from app.models import Admission, ProgressNote

    patient = _patient(client, admin, "直插验证丁", "330281198001010070")
    # 自建一张床：床位是被原子占用的，蹭别的用例的床会让本条用例的红绿取决于执行顺序
    bed = client.post(
        "/api/inpatient/beds",
        json={"ward_id": ward["ward"]["id"], "bed_no": "LU-直插"}, headers=admin,
    ).json()
    first = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["ward"]["id"],
              "bed_id": bed["id"], "doctor_name": "赵医生"},
        headers=admin,
    )
    assert first.status_code == 201, first.text

    db = SessionLocal()
    try:
        db.add(Admission(
            patient_id=patient["id"], org_id=ward["org"]["id"],
            ward_id=ward["ward"]["id"], bed_id=bed["id"],
            doctor_name="钱医生", status="admitted", created_by=1,
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        aid = first.json()["id"]
        note_kwargs = dict(admission_id=aid, note_type="first", content="首次病程",
                           recorded_at="2026-09-10 09:00", created_by=1)
        db.add(ProgressNote(**note_kwargs))
        db.commit()
        db.add(ProgressNote(**note_kwargs))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_三条索引真的建在库上():
    """模型声明了、库里没建过（漏迁移）同样等于没有约束——按真实表结构再钉一遍。"""
    inspector = sa_inspect(engine)
    for table, index_name in (
        ("admissions", "uq_admission_patient_admitted"),
        ("appointment_slots", "uq_slot_with_employee"),
        ("appointment_slots", "uq_slot_without_employee"),
        ("progress_notes", "uq_progress_note_first"),
    ):
        names = {i["name"] for i in inspector.get_indexes(table)}
        assert index_name in names, f"{table} 上没有 {index_name}（库与模型对不上）"
