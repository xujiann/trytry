"""在院患者清单按「在院」取数，不在「最新 200 条住院」里挑（P2-154）。

住院临床文书的患者选择框、医生移动端查房、住院管理页的「住院记录」三处原先都不带条件取住院列表——拿到的是
**最新 200 条**（出院的在院的都算），再在页面上挑在院的。住得久的患者被新入院的挤出前 200 条，就从三处同时消失：
病程、护理、体温单写不了，查房选不到，住院管理页上连「出院」按钮都没有；最新 200 条碰巧都出院了，页面直说
「暂无在院患者」。接口本来就收 `status`，页面没用。

按在院取也只取了第一页（P2-1333，第三十九批扫描 AC4-1）：三处都写 `status=admitted&limit=500`，而清单一页最多 500 条
（`deps.paginate` 的上限）、按住院号倒序——在院过 500 人的机构，被截掉的正是住得最久的那几位，三处同时不见了，
X-Total-Count 写着 501，页面不读、也不提示。现在三处都经 shared.js 的 `fetchAllPages` 续页取全。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
#: 取在院清单的三处页面：住院管理、住院临床文书、医生移动端查房
PAGES = (("pages-mgmt.js",), ("pages-clinical.js",), ("m", "doctor.js"))
#: 页面取在院清单的那一句：直接 `api("…")` 只取得到一页（P2-1333 修前），`fetchAllPages(api, "…")` 续页取全
IN_HOSPITAL_FETCH = re.compile(
    r'(?:fetchAllPages\(api, |\bapi\()"/api/inpatient/admissions\?status=admitted[^"]*"\)')


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.models import Admission, User

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2155 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2155 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2155-1"}).json()["id"]
    long_stay, others = (client.post("/api/patients", headers=admin, json={
        "name": f"P2155 患者{i}", "id_card": f"33010619550505{i:04d}", "gender": "女"}).json()["id"] for i in (1, 2))
    admitted = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": long_stay, "ward_id": ward, "bed_id": bed, "diagnosis_name": "脑梗死恢复期"})
    assert admitted.status_code == 201, admitted.text
    with SessionLocal() as db:   # 之后又办了 200 次住院、都已出院
        operator = db.query(User).filter(User.username == "admin").one().id
        db.add_all([Admission(patient_id=others, org_id=org, ward_id=ward, bed_id=bed, status="discharged",
                              diagnosis_name="阑尾炎", created_by=operator) for _ in range(200)])
        db.commit()
    return {"long_stay": admitted.json()["id"]}


def test_不带条件只见最新200条_按在院取才见得到住得久的(client, admin, world):
    latest = client.get("/api/inpatient/admissions", headers=admin).json()
    assert world["long_stay"] not in [a["id"] for a in latest]   # 页面原先的取法：他不在
    in_hospital = client.get("/api/inpatient/admissions?status=admitted&limit=500", headers=admin).json()
    assert [a["id"] for a in in_hospital] == [world["long_stay"]]


def _read(*parts):
    with open(os.path.join(STATIC, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_三处在院清单都按在院取数():
    for parts in PAGES:
        source = _read(*parts)
        # 在院的一个不落：按在院取、续页取全（P2-1333）；只取一页（`&limit=500`）的写法不许回来
        assert 'fetchAllPages(api, "/api/inpatient/admissions?status=admitted")' in source, "/".join(parts)
        assert "/api/inpatient/admissions?status=admitted&limit=" not in source, "/".join(parts)
        # 「取最新 N 条再在页面上挑在院」这个形状不许回来
        assert not re.search(r'api\("/api/inpatient/admissions"\)\)?\s*\.filter\(\(a\) => a\.status === "admitted"\)',
                             source), "/".join(parts)
        assert not re.search(r'api\("/api/inpatient/admissions"\);\s*\n\s*const \w+ = \w+\.filter\(\(a\) => a\.status === "admitted"\)',
                             source), "/".join(parts)


def _run_page_fetch(client, headers, expression):
    """在 node 里原样执行页面取数的那一句（先加载 shared.js，与浏览器同序），页面的 `api` 经管道转给真接口。

    返回 (取回的住院号, 依次请求过的地址)。
    """
    shared = _read("shared.js")
    script = (
        # shared.js 顶层只碰 document.addEventListener（原生提交兜底）
        "globalThis.document = { addEventListener() {}, querySelector() { return null; }, cookie: '' };\n"
        + shared
        + "\nconst rl = require('readline').createInterface({ input: process.stdin });\n"
        "const lines = rl[Symbol.asyncIterator]();\n"
        "async function api(path) {\n"
        "  process.stdout.write(JSON.stringify({ get: path }) + '\\n');\n"
        "  return JSON.parse((await lines.next()).value);\n"
        "}\n"
        f"(async () => {{ const rows = await {expression};\n"
        "  process.stdout.write(JSON.stringify({ ids: rows.map((a) => a.id) }) + '\\n'); rl.close(); })();\n"
    )
    proc = subprocess.Popen(["node", "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    requested = []
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{expression}"
            message = json.loads(line)
            if "ids" in message:
                return message["ids"], requested
            requested.append(message["get"])
            assert len(requested) <= 10, f"翻页停不下来：{requested}"
            resp = client.get(message["get"], headers=headers)
            assert resp.status_code == 200, resp.text
            proc.stdin.write(json.dumps(resp.json()) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面的取数")
def test_在院超过500人_三处按页面的取法都取得到最早入院的那一位(client, admin):
    """P2-1333（第三十九批扫描 AC4-1）：三处原先只取一页 `status=admitted&limit=500`，清单一页最多 500 条、按住院号倒序。
    实测（修前）：县医院在院 501 人，医生取回 500 条、X-Total-Count 501，最早入院的那一位不在——住院页办不了他的出院，
    住院临床文书写不了他的病程、护理、体温单，查房选不到他，页面也不说截断。在院数以床位数封顶，取全不会无界。

    这里把三处页面取在院清单的那一句原样拿到 node 里跑（先加载 shared.js），请求转给真接口。
    """
    from app.database import SessionLocal
    from app.models import Admission, Bed, Patient, User

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21333 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21333_doc", "password": "passw0rd1", "role": "doctor", "org_id": org})
    assert created.status_code in (200, 201), created.text
    login = client.post("/api/auth/login", json={"username": "p21333_doc", "password": "passw0rd1"})
    doctor = {"Authorization": f"Bearer {login.json()['access_token']}"}
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21333 病区"}).json()["id"]
    with SessionLocal() as db:   # 501 张床住满（逐个走入院接口太慢，直接落库；床位与住院一一对应）
        operator = db.query(User).filter(User.username == "p21333_doc").one().id
        patients = [Patient(ehc_no=f"P21333-{i:04d}", name=f"P21333 患者{i}", id_card=f"33010619600101{i:04d}")
                    for i in range(501)]
        beds = [Bed(ward_id=ward, bed_no=f"{i + 1:03d}", status="occupied") for i in range(501)]
        db.add_all(patients + beds)
        db.flush()
        admissions = [Admission(patient_id=p.id, org_id=org, ward_id=ward, bed_id=b.id, diagnosis_name="脑梗死恢复期",
                                created_by=operator) for p, b in zip(patients, beds)]
        db.add_all(admissions)
        db.commit()
        ids = sorted(a.id for a in admissions)
    earliest = ids[0]

    first_page = client.get("/api/inpatient/admissions?status=admitted&limit=500", headers=doctor)
    assert first_page.headers["X-Total-Count"] == "501"
    assert earliest not in [a["id"] for a in first_page.json()]   # 一页取不全：最早入院的那一位在第二页

    for parts in PAGES:
        found = IN_HOSPITAL_FETCH.search(_read(*parts))
        assert found, f"{'/'.join(parts)} 里找不到取在院清单的那一句"
        got, requested = _run_page_fetch(client, doctor, found.group(0))
        assert earliest in got, ("/".join(parts), found.group(0), requested, len(got))   # 修前 500 条、他不在
        assert got == ids[::-1], ("/".join(parts), requested)   # 一个不落、不重复，与接口同序（住院号倒序）
