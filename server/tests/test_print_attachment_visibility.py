"""P0 整改回归：打印 4 端点与附件下载/列举必须校验归属并留痕。

修的洞（CLAUDE.md §8 明令禁止的"按 id 直取、不校验归属、无留痕"）：

- `/api/print/*`：四个单据只要"登录"就渲染。脱敏做了，但脱敏挡不住"这个人
  根本不该看这张单子"——乙院账号按 id 顺序遍历就能把甲院全部报告单打出来。
- `/api/attachments/{id}`：同样只要登录就回源文件，连 AccessLog 都不写。

口径（见 `routers/attachments.OwnerSpec`）：患者类走 `assert_patient_visible`
（校验 + 留痕），机构类走机构可见性，课件类 scope="all" 登录即可。
"""
import io

import pytest
from fastapi.testclient import TestClient

from conftest import login, reset_database

from app.main import app
from app.database import SessionLocal
from app.models import AccessLog


@pytest.fixture()
def client():
    """函数级：断言涉及 AccessLog 全量计数与跨机构 403，共享库会引入顺序耦合。"""
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def world(client):
    """两家互不相干的县医院，各有自己的患者、报告、处方与医师。"""
    admin = login(client, "admin", "admin123")
    out = {"admin": admin}
    for tag in ("甲", "乙"):
        org = client.post(
            "/api/organizations",
            json={"name": f"{tag}县医院", "org_type": "lead_hospital", "level": "county"},
            headers=admin,
        ).json()
        client.post(
            "/api/users",
            json={"username": f"doc_{tag}", "password": "pass123456",
                  "role": "doctor", "org_id": org["id"]},
            headers=admin,
        )
        doc = login(client, f"doc_{tag}", "pass123456")
        patient = client.post(
            "/api/patients",
            json={"name": f"{tag}患者",
                  "id_card": "33028119910101600" + ("6" if tag == "甲" else "7")},
            headers=admin,
        ).json()
        # 就诊一次，建立"本机构与该患者有关系"的依据
        client.post(
            "/api/encounters",
            json={"patient_id": patient["id"], "org_id": org["id"], "visit_type": "outpatient"},
            headers=doc,
        )
        req = client.post(
            "/api/exams",
            json={"patient_id": patient["id"], "from_org_id": org["id"],
                  "center_type": "imaging", "item_code": "CT", "item_name": "胸部CT"},
            headers=doc,
        ).json()
        client.post(f"/api/exams/{req['id']}/claim", headers=doc)
        report = client.post(
            f"/api/exams/{req['id']}/report",
            json={"conclusion": "未见异常", "critical": False}, headers=doc,
        ).json()
        out[tag] = {"org": org, "doc": doc, "patient": patient,
                    "request": req, "report": report}
    return out


def test_共享诊断中心医师能打印与挂载自己写的报告(client, world):
    """"基层检查、上级诊断"是平台核心流程，可见性校验不能把它挡死。

    甲院开单 → **乙院（中心）医师**领取并出报告。此前 `exam_requests` 只记
    `from_org_id` 与 `claimed_by`（展示名），中心与该患者的服务关系在模型里
    没有落点，于是中心医师写完报告却 403 打不开自己写的那份——
    `claimed_org_id` 补的正是这条关系。
    """
    admin = world["admin"]
    # 甲院患者、甲院开单；乙院医师是"中心"，与该患者没有任何就诊/签约关系
    req = client.post(
        "/api/exams",
        json={"patient_id": world["甲"]["patient"]["id"], "from_org_id": world["甲"]["org"]["id"],
              "center_type": "imaging", "item_code": "MR", "item_name": "头颅MR"},
        headers=world["甲"]["doc"],
    ).json()
    assert client.post(f"/api/exams/{req['id']}/claim", headers=world["乙"]["doc"]).status_code == 200
    report = client.post(
        f"/api/exams/{req['id']}/report",
        json={"conclusion": "未见异常", "critical": False}, headers=world["乙"]["doc"],
    )
    assert report.status_code == 201, report.text
    rid = report.json()["id"]

    printed = client.get(f"/api/print/exam-reports/{rid}", headers=world["乙"]["doc"])
    assert printed.status_code == 200, f"中心医师打不开自己写的报告：{printed.text}"
    up = _upload(client, world["乙"]["doc"], "exam_report", rid)
    assert up.status_code == 201, f"中心医师挂不上影像附件：{up.text}"

    # 但与本单毫无关系的第三方仍然进不来——放行的是"中心"，不是"所有人"
    client.post(
        "/api/organizations",
        json={"name": "丙县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    )
    orgs = client.get("/api/organizations", headers=admin).json()
    third_org = next(o for o in orgs if o["name"] == "丙县医院")
    client.post(
        "/api/users",
        json={"username": "doc_丙", "password": "pass123456", "role": "doctor",
              "org_id": third_org["id"]},
        headers=admin,
    )
    third = login(client, "doc_丙", "pass123456")
    assert client.get(f"/api/print/exam-reports/{rid}", headers=third).status_code == 403


def _access_log_count() -> int:
    with SessionLocal() as db:
        return db.query(AccessLog).count()


# ---------------------------------------------------------------- 打印端点


def test_打印报告单_他院账号403_本院可打(client, world):
    url = f"/api/print/exam-reports/{world['甲']['report']['id']}"
    assert client.get(url, headers=world["甲"]["doc"]).status_code == 200
    forbidden = client.get(url, headers=world["乙"]["doc"])
    assert forbidden.status_code == 403, forbidden.text


def test_打印申请单与处方笺同样受限(client, world):
    rx = client.post(
        "/api/prescriptions",
        json={"patient_id": world["甲"]["patient"]["id"], "org_id": world["甲"]["org"]["id"],
              "diagnosis_name": "上感",
              "items": [{"drug_code": "D1", "drug_name": "阿莫西林",
                         "daily_dose": 1.5, "days": 3}]},
        headers=world["甲"]["doc"],
    )
    assert rx.status_code in (200, 201), rx.text
    rx_id = rx.json()["id"]
    for url in (
        f"/api/print/exam-requests/{world['甲']['request']['id']}",
        f"/api/print/prescriptions/{rx_id}",
    ):
        assert client.get(url, headers=world["甲"]["doc"]).status_code == 200, url
        assert client.get(url, headers=world["乙"]["doc"]).status_code == 403, url


def test_打印必须留痕(client, world):
    """能看的每一次都要记下来——留痕是事后追责的唯一依据。"""
    before = _access_log_count()
    url = f"/api/print/exam-reports/{world['甲']['report']['id']}"
    assert client.get(url, headers=world["甲"]["doc"]).status_code == 200
    assert _access_log_count() > before, "打印成功却没写 AccessLog"
    with SessionLocal() as db:
        last = db.query(AccessLog).order_by(AccessLog.id.desc()).first()
    assert last.resource == "print:exam_report"
    assert last.patient_id == world["甲"]["patient"]["id"]


# ---------------------------------------------------------------- 附件


def _upload(client, headers, owner_type, owner_id):
    return client.post(
        "/api/attachments",
        files={"file": ("x.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 32), "image/png")},
        data={"owner_type": owner_type, "owner_id": str(owner_id)},
        headers=headers,
    )


def test_患者类附件_他院账号下载403_且列举也挡住(client, world):
    up = _upload(client, world["甲"]["doc"], "exam_report", world["甲"]["report"]["id"])
    assert up.status_code == 201, up.text
    att_id = up.json()["id"]

    assert client.get(f"/api/attachments/{att_id}", headers=world["甲"]["doc"]).status_code == 200
    assert client.get(f"/api/attachments/{att_id}", headers=world["乙"]["doc"]).status_code == 403

    listing = "/api/attachments?owner_type=exam_report&owner_id=%d" % world["甲"]["report"]["id"]
    assert client.get(listing, headers=world["甲"]["doc"]).status_code == 200
    assert client.get(listing, headers=world["乙"]["doc"]).status_code == 403, \
        "文件名里常带患者姓名，只挡下载不挡列举等于把目录留在门外"


def test_患者类附件下载留痕(client, world):
    up = _upload(client, world["甲"]["doc"], "exam_report", world["甲"]["report"]["id"])
    att_id = up.json()["id"]
    before = _access_log_count()
    assert client.get(f"/api/attachments/{att_id}", headers=world["甲"]["doc"]).status_code == 200
    assert _access_log_count() > before, "附件下载成功却没写 AccessLog"


def test_他院账号不得往本院报告上挂附件(client, world):
    """角色对了不等于这份对象归你。"""
    bad = _upload(client, world["乙"]["doc"], "exam_report", world["甲"]["report"]["id"])
    assert bad.status_code == 403, bad.text


def test_课件类附件全员可下载(client, world):
    """口径：课件是面向全员的培训资料，既不含患者数据也不属于某家机构。"""
    admin = world["admin"]
    course = client.post(
        "/api/education/courses",
        json={"title": "抗菌药物合理使用", "category": "clinical"}, headers=admin,
    )
    assert course.status_code in (200, 201), course.text
    material = client.post(
        f"/api/education/courses/{course.json()['id']}/materials",
        json={"title": "第一讲", "material_type": "slide"},
        headers=admin,
    )
    assert material.status_code in (200, 201), material.text
    up = _upload(client, admin, "course_material", material.json()["id"])
    assert up.status_code == 201, up.text
    att_id = up.json()["id"]
    # 两家医院的医师都能下载——scope="all"
    for tag in ("甲", "乙"):
        r = client.get(f"/api/attachments/{att_id}", headers=world[tag]["doc"])
        assert r.status_code == 200, f"{tag}院医师应能下载课件：{r.text}"


def test_注册附件业务域必须显式声明可见性口径():
    """漏声明要在装载期炸掉，而不是安静退化成"谁都能下载"。"""
    from app.routers.attachments import OwnerSpec, register_owner
    from app.models import CourseMaterial

    with pytest.raises(TypeError):
        register_owner("bad", CourseMaterial, ("doctor",))        # 少了 scope
    with pytest.raises(ValueError):
        OwnerSpec("bad", CourseMaterial, ("doctor",), "everyone")  # 口径不在白名单
    with pytest.raises(ValueError):
        OwnerSpec("bad", CourseMaterial, ("doctor",), "patient")   # 缺 patient_of
    with pytest.raises(ValueError):
        # 机构档写错列名：getattr 取到 None、assert_org_visible 直接放行，
        # 机构校验会**静默失效**——必须在注册时就炸掉
        OwnerSpec("bad", CourseMaterial, ("doctor",), "org", org_attr="no_such_col")
    with pytest.raises(ValueError):
        # owner_type 过长 → resource 串超过 AccessLog.resource 的 32 字符，
        # 而留痕失败是被吞掉的，结果是"看了但没留痕"
        OwnerSpec("a_very_long_owner_type_name", CourseMaterial, ("doctor",),
                  "patient", patient_of=lambda db, o: 1)


def test_所有已注册业务域的留痕串都在列宽内():
    """`_write_access_log` 会吞掉落库失败——超长不会报错，只会静默丢审计。

    所以把长度检查放到测试里：新接业务域时若 owner_type 起长了，这里先红。
    """
    from app.models import AccessLog
    from app.routers.attachments import ACTIONS, _OWNERS, _resource

    limit = AccessLog.__table__.columns["resource"].type.length
    for owner_type, spec in _OWNERS.items():
        if spec.scope != "patient":
            continue        # 只有患者档会写 AccessLog
        for action in ACTIONS:
            value = _resource(owner_type, action)
            assert len(value) <= limit, f"{value} 超过 resource 列宽 {limit}"
