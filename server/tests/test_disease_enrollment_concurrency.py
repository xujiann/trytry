"""专病入组"同患者同专病在管唯一"下沉到库之后的回归（P1-30）。

洞的形状：`disease_programs.enroll` 是"先查有没有在管、没有就建"，两步之间
没有闸门。并发下两路都查不到，就都建——**不报错，静默写出两条在管记录**。
后果不是"多一行"：`program_stats` 把同一个人算两遍，`/exit` 只翻掉其中一条，
剩下那条永远是 `enrolled`，于是这个患者**再也入不了组**（预检永远命中它），
而"复发再入组"恰恰是这个模块存在的理由。

`uq_disease_enrollment_program_patient_enrolled`（部分唯一索引，仅锁
`status = 'enrolled'`）把这条不变式下沉到库，接口层改走 `insert_or_conflict`。
本档钉三件事：

1. **行为面**：顺序重复仍是同一句 409（文案逐字相同——对调用方来说"并发撞车"
   与"本来就重复"没有区别）；出组后复发再入组仍是 201。
2. **防拆卸**：索引必须留在模型上、带 `unique=True`、且**是部分索引**——
   写成全量唯一会把"出组后复发再入组"一并拒掉，那是另一种坏。库上也要真有它。
3. **兜底真的生效**：绕开接口层直插，看数据库自己抬不抬手。这条不可省——
   顺序请求下预检就给了 409，行为用例**分辨不出**兜底是否还在；而 SQLite 的
   库级写锁又让线程探针对"索引被拆掉"不敏感（拆了照样大概率不重复）。
   真并发下的一赢七输在 `test_disease_enrollment_unique_races.py`（真 PG）。
"""
import pytest
from sqlalchemy import inspect as sa_inspect

from conftest import login

from app.database import engine
from app.models import Base

# 接口层与并发兜底必须给出同一句话；两处文案漂了，只有真并发的输家会看到旧话，
# 而那条路径在 SQLite 上跑不到——所以断言的是 detail 本身，不只是 409。
ALREADY_ENROLLED = "该患者已在本专病在管中"


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "专病入组并发县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def doctor(client, admin, org):
    """入组端点限 doctor/public_health（既有业务规则），且 `assert_org_writable`
    要求 body.org_id 就是本人机构——单开一个本机构医师账号，别蹭 admin 的全域身份。"""
    client.post(
        "/api/users",
        json={"username": "de_doc", "password": "pass123456", "role": "doctor",
              "org_id": org["id"], "full_name": "邓医生"},
        headers=admin,
    )
    return login(client, "de_doc", "pass123456")


@pytest.fixture(scope="module")
def program(client, admin, org):
    return client.post(
        "/api/disease-programs",
        json={"code": "DE-CONC", "name": "并发验证专病", "org_id": org["id"],
              "path_nodes": [{"key": "assess", "name": "首次评估", "required": True}]},
        headers=admin,
    ).json()


def _patient(client, admin, name, id_card):
    resp = client.post("/api/patients", json={"name": name, "id_card": id_card}, headers=admin)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _enroll(client, doctor, program, patient, org):
    return client.post(
        f"/api/disease-programs/{program['id']}/enrollments",
        json={"patient_id": patient["id"], "org_id": org["id"]},
        headers=doctor,
    )


# ==================================================================== 行为面


def test_同患者同专病重复入组仍是同一句409(client, admin, doctor, program, org):
    """下沉到库不许改调用方看到的东西：状态码与文案都逐字照旧。"""
    patient = _patient(client, admin, "并发入组甲", "330281199003030011")
    first = _enroll(client, doctor, program, patient, org)
    assert first.status_code == 201, first.text

    second = _enroll(client, doctor, program, patient, org)
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == ALREADY_ENROLLED


def test_出组后复发再入组仍然放行(client, admin, doctor, program, org):
    """索引只锁 `enrolled` 一态就是为了这条：治好出组之后又犯是常态。

    全量唯一（不带 WHERE）在这里会拒掉第二次入组——那不是"更严"，是坏掉。
    """
    patient = _patient(client, admin, "并发入组乙", "330281199003030029")
    first = _enroll(client, doctor, program, patient, org)
    assert first.status_code == 201, first.text

    exited = client.post(
        f"/api/disease-programs/enrollments/{first.json()['id']}/exit",
        json={"status": "exited", "exit_reason": "转上级医院继续治疗"},
        headers=doctor,
    )
    assert exited.status_code == 200, exited.text

    again = _enroll(client, doctor, program, patient, org)
    assert again.status_code == 201, again.text
    assert again.json()["status"] == "enrolled"
    assert again.json()["id"] != first.json()["id"]


def test_成功入组的响应字段一字未改(client, admin, doctor, program, org):
    """`insert_or_conflict` 内部是 commit + refresh，与原来的 `add`/`commit` 等价；
    但"等价"要有证据——把键序与首建时的取值钉下来，免得将来换写法悄悄改了响应。"""
    patient = _patient(client, admin, "并发入组丙", "330281199003030037")
    body = _enroll(client, doctor, program, patient, org).json()
    assert list(body) == [
        "id", "program_id", "patient_id", "org_id", "status", "status_name",
        "enrolled_at", "exited_at", "outcome", "outcome_name", "outcome_note",
        "exit_reason", "completion", "records",
    ]
    assert body["status"] == "enrolled" and body["status_name"] == "在管"
    assert body["program_id"] == program["id"] and body["patient_id"] == patient["id"]
    assert body["org_id"] == org["id"] and body["records"] == []


# ============================================================== 防拆卸静态钉


def test_在管唯一的部分索引不许消失():
    """模型侧的声明就是这条不变式的落点，删掉就等于把静默双写的洞放回去。

    同时钉住"是部分索引"：条件里必须还有 `enrolled`——写成全量唯一会拒掉
    合法的复发再入组。
    """
    index = next(
        (
            i
            for i in Base.metadata.tables["disease_enrollments"].indexes
            if i.name == "uq_disease_enrollment_program_patient_enrolled"
        ),
        None,
    )
    assert index is not None, "uq_disease_enrollment_program_patient_enrolled 没了——静默双写的洞回来了"
    assert index.unique, "该索引不再是唯一索引，等于没有约束"
    assert [c.name for c in index.columns] == ["program_id", "patient_id"], "索引的键变了"
    for dialect in ("sqlite", "postgresql"):
        where = str(index.dialect_options[dialect].get("where", ""))
        assert "enrolled" in where, (
            f"{dialect} 侧的部分条件不再包含 'enrolled'：全量唯一会拒掉出组后的复发再入组"
        )


def test_在管唯一的索引真的建在库上():
    """模型声明了、库里没建过（漏迁移）同样等于没有约束——按真实表结构再钉一遍。"""
    names = {i["name"] for i in sa_inspect(engine).get_indexes("disease_enrollments")}
    assert "uq_disease_enrollment_program_patient_enrolled" in names, (
        "disease_enrollments 上没有 uq_disease_enrollment_program_patient_enrolled（库与模型对不上）"
    )


def test_绕开接口层直插时库里真的拦得住(client, admin, doctor, program, org):
    """索引"在不在"与"拦不拦得住"是两回事。

    顺序请求下预检就会给出 409，上面的行为用例因此分辨不出兜底是否真的生效；
    SQLite 的库级写锁又让线程探针对拆卸不敏感。这里绕开接口层直接写库——
    那正是并发抢输者实际到达的位置——看数据库自己是否抬手。
    """
    from sqlalchemy.exc import IntegrityError

    from app.database import SessionLocal
    from app.models import DiseaseEnrollment

    patient = _patient(client, admin, "直插验证丁", "330281199003030045")
    first = _enroll(client, doctor, program, patient, org)
    assert first.status_code == 201, first.text

    def row(status):
        return DiseaseEnrollment(
            program_id=program["id"], patient_id=patient["id"], org_id=org["id"],
            status=status, enrolled_at="2026-09-05", created_by=1,
        )

    db = SessionLocal()
    try:
        db.add(row("enrolled"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # 出组态不在索引条件内：历史可以有很多条，索引不该拦它们
        db.add(row("exited"))
        db.add(row("completed"))
        db.commit()
    finally:
        db.close()
