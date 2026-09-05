"""同一次住院里"内容相同的执行中长期医嘱只能有一条"下沉到库之后的回归（P1-30）。

洞的形状与 P1-29 那三条同源：`create_order` 只判了"患者是否在院"，重复开立
**连预检都没有**——双击一次、或两台工作站同时开，就是两行一模一样的长期医嘱。
下游全都挂在医嘱 id 上（执行登记、护理记录），于是同一味药出两条 MAR 行、
护士按两条各执行一次，最后要主管医师回头人工仲裁停掉一条。

范围是刻意窄的（`order_type='long' AND status='active'`）：临时医嘱按次开立
（换药、皮试同内容多条合法），长期医嘱停用后重开也合法——写成全表唯一会拒掉
这两类正常业务。

本档钉三件事，分工同 `tests/test_logical_unique_races.py`：

1. **行为面**：顺序重复拿 409、文案与并发抢输完全一致；被排除的三类（临时、
   已停用、别的住院/别的内容）照旧 201；
2. **顺序面**：出院后开医嘱仍先拿"患者已出院"，查重预检不得插到它前面；
3. **防拆卸**：索引要留在模型上、也要真的建在库上；再绕开接口层直插一条，
   看数据库自己抬不抬手——SQLite 的库级写锁让线程探针对"拆掉索引"不敏感，
   静态钉 + 直插才是确定性的网（真并发在 `test_inpatient_order_unique_races.py`）。
"""
import pytest
from sqlalchemy import inspect as sa_inspect

from conftest import login

from app.database import engine
from app.models import Base

LONG_CONTENT = "头孢曲松 2g qd ivgtt"


@pytest.fixture(scope="module")
def ward(client, admin):
    """一个机构 + 病区 + 若干床：每条用例占各自的床，互不串味。"""
    org = client.post(
        "/api/organizations",
        json={"name": "长嘱唯一县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    ward = client.post(
        "/api/inpatient/wards",
        json={"org_id": org["id"], "name": "呼吸内科", "ward_type": "general"},
        headers=admin,
    ).json()
    beds = [
        client.post(
            "/api/inpatient/beds",
            json={"ward_id": ward["id"], "bed_no": f"IO-{i}"},
            headers=admin,
        ).json()
        for i in range(1, 8)
    ]
    return {"org": org, "ward": ward, "beds": beds}


@pytest.fixture(scope="module")
def doctor(client, admin, ward):
    """医嘱开立/停止限 doctor 角色（既有业务规则），单开一个本机构医师账号。"""
    client.post(
        "/api/users",
        json={"username": "io_doc", "password": "pass123456", "role": "doctor",
              "org_id": ward["org"]["id"], "full_name": "殷医生"},
        headers=admin,
    )
    return login(client, "io_doc", "pass123456")


def _admit(client, admin, ward, name, id_card, bed_index):
    patient = client.post(
        "/api/patients",
        json={"name": name, "id_card": id_card, "gender": "男", "birth_date": "1979-03-03"},
        headers=admin,
    ).json()
    created = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["ward"]["id"],
              "bed_id": ward["beds"][bed_index]["id"], "doctor_name": "殷医生"},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    return patient, created.json()


def _order(client, doctor, admission_id, order_type, content):
    return client.post(
        "/api/inpatient/orders",
        json={"admission_id": admission_id, "order_type": order_type, "content": content},
        headers=doctor,
    )


# ================================================================ 行为面


def test_同住院同内容长期医嘱第二条409(client, admin, ward, doctor):
    _, admission = _admit(client, admin, ward, "长嘱甲", "330281197903030016", 0)
    first = _order(client, doctor, admission["id"], "long", LONG_CONTENT)
    assert first.status_code == 201, first.text

    again = _order(client, doctor, admission["id"], "long", LONG_CONTENT)
    assert again.status_code == 409
    assert again.json()["detail"] == "该住院已有内容相同的执行中长期医嘱，请先停止原医嘱再开立"

    listed = client.get(
        "/api/inpatient/orders",
        params={"admission_id": admission["id"], "status": "active"},
        headers=doctor,
    ).json()
    same = [o for o in listed if o["content"] == LONG_CONTENT]
    assert len(same) == 1, "静默写出两条一模一样的在执行长期医嘱正是本次要防的事"


def test_临时医嘱同内容可多条(client, admin, ward, doctor):
    """临时医嘱按次开立：换药、皮试同内容反复开是正常业务，索引不该锁到它头上。"""
    _, admission = _admit(client, admin, ward, "长嘱乙", "330281197903030032", 1)
    for _ in range(2):
        resp = _order(client, doctor, admission["id"], "temp", "换药一次")
        assert resp.status_code == 201, resp.text
    listed = client.get(
        "/api/inpatient/orders", params={"admission_id": admission["id"]}, headers=doctor
    ).json()
    assert len([o for o in listed if o["content"] == "换药一次"]) == 2


def test_停用后可重开同内容长期医嘱(client, admin, ward, doctor):
    """唯一性只约束"执行中"这一态：写成不带 status 条件的唯一索引，这条会红。"""
    _, admission = _admit(client, admin, ward, "长嘱丙", "330281197903030059", 2)
    first = _order(client, doctor, admission["id"], "long", LONG_CONTENT)
    assert first.status_code == 201, first.text

    stopped = client.post(
        f"/api/inpatient/orders/{first.json()['id']}/stop", headers=doctor
    )
    assert stopped.status_code == 200, stopped.text

    reissued = _order(client, doctor, admission["id"], "long", LONG_CONTENT)
    assert reissued.status_code == 201, reissued.text
    # 重开之后这一态又满了，第三条照旧 409
    third = _order(client, doctor, admission["id"], "long", LONG_CONTENT)
    assert third.status_code == 409
    assert third.json()["detail"].startswith("该住院已有内容相同的执行中长期医嘱")


def test_不同住院或不同内容互不冲突(client, admin, ward, doctor):
    _, first_admission = _admit(client, admin, ward, "长嘱丁", "330281197903030075", 3)
    _, other_admission = _admit(client, admin, ward, "长嘱戊", "330281197903030091", 4)
    assert _order(client, doctor, first_admission["id"], "long", LONG_CONTENT).status_code == 201
    # 换一次住院：同内容合法
    assert _order(client, doctor, other_admission["id"], "long", LONG_CONTENT).status_code == 201
    # 换内容（哪怕只差频次）：同住院合法
    assert _order(
        client, doctor, first_admission["id"], "long", "头孢曲松 2g bid ivgtt"
    ).status_code == 201


def test_出院后开医嘱仍先拿患者已出院(client, admin, ward, doctor):
    """查重预检必须排在"是否在院"之后：插到前面会把这句 409 换成查重那句。"""
    _, admission = _admit(client, admin, ward, "长嘱己", "330281197903030113", 5)
    assert _order(client, doctor, admission["id"], "long", LONG_CONTENT).status_code == 201
    summary = client.post(
        f"/api/inpatient/admissions/{admission['id']}/case-summary",
        json={"discharge_diagnosis": "肺部感染", "outcome": "好转"},
        headers=doctor,
    )
    assert summary.status_code == 201, summary.text
    assert client.post(
        f"/api/inpatient/admissions/{admission['id']}/discharge", headers=doctor
    ).status_code == 200
    # 出院把医嘱批量停成 stopped，索引这一态已空——此时仍必须是"患者已出院"
    resp = _order(client, doctor, admission["id"], "long", LONG_CONTENT)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "患者已出院，不可开立医嘱"


# ================================================================ 防拆卸静态钉


def test_长期医嘱部分唯一索引不许消失():
    """模型侧的声明就是这条不变式的落点，删掉就等于把洞放回去。

    同时钉住"是部分索引"——写成全量唯一会拒掉合法的多条（临时医嘱同内容多次、
    停用后重开），那是另一种坏。
    """
    index = next(
        (i for i in Base.metadata.tables["inpatient_orders"].indexes
         if i.name == "uq_inpatient_order_active_long"),
        None,
    )
    assert index is not None, "inpatient_orders 的 uq_inpatient_order_active_long 没了——静默双写的洞回来了"
    assert index.unique, "uq_inpatient_order_active_long 不再是唯一索引，等于没有约束"
    assert [c.name for c in index.columns] == ["admission_id", "content"], "索引的键变了"
    for dialect in ("sqlite", "postgresql"):
        where = str(index.dialect_options[dialect].get("where", ""))
        assert "order_type = 'long'" in where, f"{dialect} 侧丢了 long 范围：临时医嘱会被误伤"
        assert "status = 'active'" in where, f"{dialect} 侧丢了 active 范围：停用后重开会被拒"


def test_长期医嘱索引真的建在库上():
    """模型声明了、库里没建过（漏迁移）同样等于没有约束——按真实表结构再钉一遍。"""
    names = {i["name"] for i in sa_inspect(engine).get_indexes("inpatient_orders")}
    assert "uq_inpatient_order_active_long" in names, "inpatient_orders 上没有这条索引（库与模型对不上）"


def test_绕开接口层直插长期医嘱时库里真的拦得住(client, admin, ward, doctor):
    """索引"在不在"与"拦不拦得住"是两回事。

    接口层的预检在顺序请求下就会给出 409，行为用例因此**分辨不出**兜底是否真的
    生效（SQLite 的库级写锁又让线程探针对拆卸不敏感）。这里绕开接口层直接写库
    ——那正是并发抢输者实际到达的位置——看数据库自己是否抬手。
    """
    from sqlalchemy.exc import IntegrityError

    from app.database import SessionLocal
    from app.models import InpatientOrder

    bed = client.post(
        "/api/inpatient/beds",
        json={"ward_id": ward["ward"]["id"], "bed_no": "IO-直插"}, headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "长嘱直插", "id_card": "330281197903030130", "gender": "男",
              "birth_date": "1979-03-03"},
        headers=admin,
    ).json()
    created = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["ward"]["id"],
              "bed_id": bed["id"], "doctor_name": "殷医生"},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    aid = created.json()["id"]

    db = SessionLocal()
    try:
        kwargs = dict(admission_id=aid, order_type="long", content=LONG_CONTENT, status="active")
        db.add(InpatientOrder(**kwargs))
        db.commit()
        db.add(InpatientOrder(**kwargs))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # 被排除的两类必须真的插得进去：索引写成全量唯一时这两句会红
        db.add(InpatientOrder(admission_id=aid, order_type="temp", content=LONG_CONTENT,
                              status="active"))
        db.add(InpatientOrder(admission_id=aid, order_type="long", content=LONG_CONTENT,
                              status="stopped"))
        db.commit()
    finally:
        db.close()
