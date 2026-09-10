"""住院临床文书的归属校验与留痕（2026-09-10 实测取证后补）。

## 修的是什么

`/api/inpatient/admissions/{admission_id}/…` 这一族七个端点，原先一律
**按 id 直取、不校验归属、不留痕**——正是 CLAUDE.md §8 原文禁止的形状。
实测取证（乙院医生，与该患者毫无业务关系）：

    GET  /admissions/1/vitals                → 200  体温 38.5 / 脉搏 90
    GET  /admissions/1/progress-notes        → 200
    GET  /admissions/1/nursing-records       → 200
    GET  /admissions/1/document-completeness → 200
    POST /admissions/1/progress-notes        → 201  **写进了别家的病程记录**
    POST /admissions/1/vitals                → 201

病程记录是法定病历，越权**写入**比越权读更严重。

## 为什么闸门一直没报

横向越权闸门（读侧与写侧）的分母只看**端点自身函数体**里的
`db.get(带 patient_id 的模型, …)`。而这七个端点的取行都在本模块的
`_admission_or_404` 里——于是整族掉出分母，闸门照样报 95.5% 覆盖率。

「判据只认一种写法」这条线上的第三例（前两例：`async def` 不是 `FunctionDef`、
`db.query(M).filter(M.id==x).first()` 不是 `db.get`）。闸门已同步跟进一层本模块调用，
见 `test_stage15_horizontal.py`。

## 这份用例守什么

**两个方向都钉**：无关机构一律 403（七条），有业务关系的照常 200/201（否则
"修好了"可能只是"全关了"）；放行的那次必须留下 `AccessLog`。
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
def ward_world(client):
    """甲乙两院各一名医师；患者只在甲院有就诊记录，并在甲院住院。"""
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "文书甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "文书乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org in (("cd_doc_a", a), ("cd_doc_b", b)):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": "doctor", "org_id": org["id"]},
                    headers=admin)
    doc_a, doc_b = _login(client, "cd_doc_a"), _login(client, "cd_doc_b")
    patient = client.post("/api/patients",
                          json={"name": "文书患者", "id_card": "320000199001011234"},
                          headers=admin).json()
    client.post("/api/encounters",
                json={"patient_id": patient["id"], "org_id": a["id"],
                      "encounter_type": "inpatient"},
                headers=doc_a)
    ward = client.post("/api/inpatient/wards",
                       json={"name": "文书病区", "org_id": a["id"]}, headers=admin).json()
    bed = client.post("/api/inpatient/beds",
                      json={"ward_id": ward["id"], "bed_no": "W-01"}, headers=admin).json()
    adm = client.post("/api/inpatient/admissions",
                      json={"patient_id": patient["id"], "org_id": a["id"],
                            "ward_id": ward["id"], "bed_id": bed["id"],
                            "doctor_name": "甲医生", "diagnosis_name": "肺炎"},
                      headers=doc_a)
    assert adm.status_code == 201, adm.text
    return {"admin": admin, "doc_a": doc_a, "doc_b": doc_b,
            "patient": patient, "admission_id": adm.json()["id"]}


READ_PATHS = ("vitals", "progress-notes", "nursing-records", "document-completeness")


def test_有业务关系的机构照常读写(client, ward_world):
    """先证明**放行的那条路是通的**——否则下面的 403 可能只是"全关了"。"""
    aid = ward_world["admission_id"]
    doc_a = ward_world["doc_a"]
    assert client.post(f"/api/inpatient/admissions/{aid}/vitals",
                       json={"temperature": 38.5, "pulse": 90,
                             "measured_at": "2026-09-10 08:00"},
                       headers=doc_a).status_code == 201
    assert client.post(f"/api/inpatient/admissions/{aid}/progress-notes",
                       json={"note_type": "daily", "content": "查房记录"},
                       headers=doc_a).status_code == 201
    assert client.post(f"/api/inpatient/admissions/{aid}/nursing-records",
                       json={"content": "一级护理", "nursing_level": "level1"},
                       headers=doc_a).status_code == 201
    for path in READ_PATHS:
        r = client.get(f"/api/inpatient/admissions/{aid}/{path}", headers=doc_a)
        assert r.status_code == 200, f"{path} 对本院医师应当放行：{r.status_code} {r.text}"


@pytest.mark.parametrize("path", READ_PATHS)
def test_无关机构不得按id直读住院文书(client, ward_world, path):
    r = client.get(f"/api/inpatient/admissions/{ward_world['admission_id']}/{path}",
                   headers=ward_world["doc_b"])
    assert r.status_code == 403, (
        f"乙院与该患者无任何业务关系，却读到了 {path}：{r.status_code} {r.text[:200]}"
    )


def test_无关机构不得写入别家的住院文书(client, ward_world):
    """越权**写**比越权读更严重：病程记录是法定病历。"""
    aid = ward_world["admission_id"]
    doc_b = ward_world["doc_b"]
    cases = [
        ("progress-notes", {"note_type": "daily", "content": "乙院越权写入"}),
        ("vitals", {"temperature": 40.0, "measured_at": "2026-09-10 09:00"}),
        ("nursing-records", {"content": "乙院越权护理记录", "nursing_level": "level1"}),
    ]
    for path, body in cases:
        r = client.post(f"/api/inpatient/admissions/{aid}/{path}", json=body, headers=doc_b)
        assert r.status_code == 403, f"乙院写入 {path} 未被拒：{r.status_code} {r.text[:200]}"


def test_放行的调阅必须留痕(client, ward_world):
    """判定与留痕是同一个动作（visibility.py 的口径）——能看的每一次都要记下来。"""
    aid, patient_id = ward_world["admission_id"], ward_world["patient"]["id"]
    client.get(f"/api/inpatient/admissions/{aid}/vitals", headers=ward_world["doc_a"])
    with SessionLocal() as db:
        rows = (
            db.query(AccessLog)
            .filter(AccessLog.patient_id == patient_id, AccessLog.resource == "vital_sign")
            .all()
        )
    assert rows, "本院医师读体温单没有留下 AccessLog——判定与留痕又分家了"


def test_被拒的调阅不留痕(client, ward_world):
    """与横向越权闸门同一口径：拒绝不写留痕，免得把"没看到"记成"看过"。"""
    aid = ward_world["admission_id"]
    client.get(f"/api/inpatient/admissions/{aid}/progress-notes", headers=ward_world["doc_b"])
    with SessionLocal() as db:
        denied = (
            db.query(AccessLog)
            .filter(AccessLog.resource == "progress_note", AccessLog.basis == "")
            .count()
        )
    assert denied == 0, "被拒的调阅不该留下留痕行"
