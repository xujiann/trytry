"""机构在**父行**上的那几张表，也要算作服务关系（P1-35）。

`patient_basis` 的推导面是"同时带 `patient_id` 与机构外键的表"。有两张表明明
是实打实的服务关系，却因为机构不在自己行上而一直进不来：

- `appointments`：号源属于哪家机构，这次预约就是哪家在服务他；
- `spd_group_members`：群组属于哪家机构，把他拉进群就是哪家在管他。

不接上的后果很具体：**居民在 X 院挂了号、还没就诊**（没有 `Encounter`、没有任何
其它记录），医生叫号时想先看既往档案——打不开，而界面上只会说"无权查看"。
这正是模块注释里那条"风险不对等"说的方向：错拦一次是诊疗事故。

这里连着两头钉：判定（`patient_basis`）与清单（`visible_patient_ids`）必须
同时认。两处不同步会出一种更难查的毛病——档案打得开，患者却不在清单里。
"""
import pytest

from conftest import reset_database

from app.database import SessionLocal
from app.main import app  # noqa: F401  触发全部模型 import（含 spd）
from app.models import Appointment, AppointmentSlot, Organization, Patient, User
from app.spd.models import SpdGroup, SpdGroupMember
from app.visibility import clear_visibility_cache, patient_basis, visible_patient_ids


@pytest.fixture()
def world():
    reset_database()
    db = SessionLocal()
    try:
        a = Organization(name="甲卫生院", org_type="township", level="township")
        b = Organization(name="乙卫生院", org_type="township", level="township")
        db.add_all([a, b])
        db.flush()
        doc_a = User(username="via_doc_a", password_hash="x", role="doctor", org_id=a.id)
        doc_b = User(username="via_doc_b", password_hash="x", role="doctor", org_id=b.id)
        patient = Patient(name="只挂过号的人", id_card="330281199203036001",
                          ehc_no="EHC-VIA-00001")
        db.add_all([doc_a, doc_b, patient])
        db.commit()
        yield {"db": db, "a": a.id, "b": b.id,
               "doc_a": doc_a, "doc_b": doc_b, "patient": patient.id}
    finally:
        db.close()


def _basis(world, who):
    clear_visibility_cache()  # 判定走 TTL 缓存，逐步断言前必须清掉
    return patient_basis(world["db"], world[who], world["patient"])


def _in_list(world, who) -> bool:
    sub = visible_patient_ids(world["db"], world[who])
    return world["patient"] in {row[0] for row in world["db"].execute(sub).all()}


def test_挂了号还没就诊_本机构医生就能看档案(world):
    db = world["db"]
    # 起点：这个人在平台上一条记录都没有
    assert _basis(world, "doc_a") is None
    assert not _in_list(world, "doc_a")

    slot = AppointmentSlot(org_id=world["a"], resource_type="clinic",
                           resource_name="全科门诊", slot_date="2026-09-22", capacity=5)
    db.add(slot)
    db.flush()
    db.add(Appointment(slot_id=slot.id, patient_id=world["patient"], status="booked"))
    db.commit()

    assert _basis(world, "doc_a") == "service", (
        "挂了本院的号却打不开档案——医生叫号时看不到既往史，"
        "而这条关系明明记在 appointments 里，只是机构在号源行上"
    )
    assert _in_list(world, "doc_a"), "判定与清单必须同时认，否则档案打得开人却不在清单里"

    # 别家机构不因为他在甲院挂号而获得依据
    assert _basis(world, "doc_b") is None
    assert not _in_list(world, "doc_b")


def test_被拉进本机构的慢专病群组_也算服务关系(world):
    db = world["db"]
    assert _basis(world, "doc_a") is None

    group = SpdGroup(name="甲院高血压群", owner_user_id=world["doc_a"].id, org_id=world["a"])
    db.add(group)
    db.flush()
    db.add(SpdGroupMember(group_id=group.id, patient_id=world["patient"]))
    db.commit()

    assert _basis(world, "doc_a") == "service"
    assert _in_list(world, "doc_a")
    assert _basis(world, "doc_b") is None


def test_群组没挂机构时不构成任何依据(world):
    """`spd_groups.org_id` 可空（个人分组）。父行没有机构 = 推不出服务机构，

    这时**只能不给依据**：拿 NULL 去比对会退化成"和其它同样没机构的行算作有关系"，
    那是静默放行（模块里 `org_id is None` 那段防的是同一件事）。
    """
    db = world["db"]
    group = SpdGroup(name="某人的个人分组", owner_user_id=world["doc_a"].id, org_id=None)
    db.add(group)
    db.flush()
    db.add(SpdGroupMember(group_id=group.id, patient_id=world["patient"]))
    db.commit()

    assert _basis(world, "doc_a") is None
    assert _basis(world, "doc_b") is None
