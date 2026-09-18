"""临床写端点的机构归属校验：住院 / 医嘱 / 手术申请 / 交接班（2026-09-18，P1-60 第二批）。

## 实测取证（修之前，乙院 doctor 对甲院的病区与住院病人）

    POST /api/inpatient/admissions   → 201  把患者收进**甲院病区**并占掉 C-02 床
                                            （回执 org_id 就是甲院）
    POST /api/inpatient/orders       → 201  给甲院的住院病人开了一条长期医嘱
    POST /api/surgery/requests       → 201  给甲院的住院病人申请手术（org_id 甲院）
    POST /api/inpatient/handovers    → 201  给甲院病区写交接班，且回执里的
                                            `patient_count` 是按甲院当前在院数现算的
                                            ——顺带把别家的在院人数读了出来

钱那一族（P1-60 第一批）丢的是账，这一族丢的是**诊疗行为的归属**：
医嘱与手术申请是临床责任的载体，写进别家的医嘱单意味着那家医院的病人身上
多了一条不是他们开的长期医嘱。

## 为什么不是转诊 / 会诊的合法形态

- **入院**：转诊由**接收方**自己办入院，交接走 `referrals` 那套；
  "别家把病人塞进我的病区并占我的床"不是转诊的任何一个步骤。
- **医嘱 / 手术**：跨机构的临床协同有专门入口（远程会诊 `telemedicine`、
  会诊申请 `consultations`），不是直接写别家的医嘱单。
- `surgery.create_request` 的 docstring 自己写着"患者与机构从住院记录带出，
  不让客户端自报，避免张冠李戴"——**带出来了，但没校验调用方是不是那家**。
  防的是客户端谎报，没防调用方越界。

## 同一批里有四条**故意没修**

`appointments.create_slot` / `inpatient.create_bed`（都是 `require_admin`）与
`cost.upsert_department_cost` / `cost.create_allocation_rule`（都是 `require_roles("director")`）
的角色门只允许全域角色（`visibility.GLOBAL_ROLES = {"admin", "director"}`），
**非全域角色连角色门都过不去，不是可越权入口**。把它们当洞去修，会写出四条
触发不了的"漏洞修复"。本文件末尾有一条用例把这个判定钉住，免得下一轮又当成欠账。
"""
import ast

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _login(client, username, password="pw123456"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def clin(client, admin):
    a = client.post("/api/organizations",
                    json={"name": "临床甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "临床乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org, role in (("clin_doc_a", a, "doctor"), ("clin_doc_b", b, "doctor")):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": role, "org_id": org["id"]},
                    headers=admin)
    ward = client.post("/api/inpatient/wards",
                       json={"org_id": a["id"], "name": "临床甲院一病区"}, headers=admin).json()
    return {"admin": admin, "a": a, "b": b, "ward": ward,
            "doc_a": _login(client, "clin_doc_a"), "doc_b": _login(client, "clin_doc_b")}


_seq = [0]


def _bed(client, clin):
    _seq[0] += 1
    return client.post("/api/inpatient/beds",
                       json={"ward_id": clin["ward"]["id"], "bed_no": f"CL-{_seq[0]:02d}"},
                       headers=clin["admin"]).json()


def _patient(client, clin):
    _seq[0] += 1
    return client.post("/api/patients",
                       json={"name": f"临床患者{_seq[0]}",
                             "id_card": f"3300001990010177{_seq[0]:02d}"},
                       headers=clin["admin"]).json()


def _admit(client, clin):
    patient, bed = _patient(client, clin), _bed(client, clin)
    resp = client.post("/api/inpatient/admissions",
                       json={"patient_id": patient["id"], "ward_id": clin["ward"]["id"],
                             "bed_id": bed["id"], "doctor_name": "王医师",
                             "diagnosis_name": "肺炎"},
                       headers=clin["admin"])
    assert resp.status_code == 201, resp.text
    return resp.json()


DENIED = {"detail": "无权以该机构名义写入数据"}


# ---------------------------------------------------------------- 越权反例


def test_别家doctor不能把患者收进本院病区(client, clin):
    patient, bed = _patient(client, clin), _bed(client, clin)
    resp = client.post("/api/inpatient/admissions", headers=clin["doc_b"],
                       json={"patient_id": patient["id"], "ward_id": clin["ward"]["id"],
                             "bed_id": bed["id"], "doctor_name": "乙院医师",
                             "diagnosis_name": "乙院写的诊断"})
    assert resp.status_code == 403, resp.text
    assert resp.json() == DENIED
    # 床没有被占掉
    beds = client.get(f"/api/inpatient/beds?ward_id={clin['ward']['id']}",
                      headers=clin["admin"]).json()
    assert [x for x in beds if x["id"] == bed["id"]][0]["status"] != "occupied"


def test_别家doctor不能给本院住院病人开医嘱(client, clin):
    adm = _admit(client, clin)
    resp = client.post("/api/inpatient/orders", headers=clin["doc_b"],
                       json={"admission_id": adm["id"], "order_type": "long",
                             "content": "乙院医师开的长期医嘱", "frequency": "qd"})
    assert resp.status_code == 403, resp.text
    assert resp.json() == DENIED


def test_别家doctor不能给本院住院病人申请手术(client, clin):
    adm = _admit(client, clin)
    resp = client.post("/api/surgery/requests", headers=clin["doc_b"],
                       json={"admission_id": adm["id"], "surgery_name": "乙院申请的手术",
                             "surgery_level": "three", "planned_date": "2026-04-01"})
    assert resp.status_code == 403, resp.text
    assert resp.json() == DENIED


def test_别家doctor不能给本院病区写交接班(client, clin):
    resp = client.post("/api/inpatient/handovers", headers=clin["doc_b"],
                       json={"ward_id": clin["ward"]["id"], "shift": "day",
                             "handover_date": "2026-04-01", "handover_from": "乙院医师",
                             "handover_to": "甲院医师", "content": "乙院写的交接班"})
    assert resp.status_code == 403, resp.text
    assert resp.json() == DENIED


# ---------------------------------------------------------------- 正路没被堵死


def test_本院doctor的临床动作照常放行(client, clin):
    """反向证据：四条各走一遍，用本用例自己造的数据。"""
    patient, bed = _patient(client, clin), _bed(client, clin)
    adm = client.post("/api/inpatient/admissions", headers=clin["doc_a"],
                      json={"patient_id": patient["id"], "ward_id": clin["ward"]["id"],
                            "bed_id": bed["id"], "doctor_name": "甲院医师",
                            "diagnosis_name": "肺炎"})
    assert adm.status_code == 201, adm.text

    order = client.post("/api/inpatient/orders", headers=clin["doc_a"],
                        json={"admission_id": adm.json()["id"], "order_type": "long",
                              "content": "甲院医师开的长期医嘱", "frequency": "qd"})
    assert order.status_code == 201, order.text

    surg = client.post("/api/surgery/requests", headers=clin["doc_a"],
                       json={"admission_id": adm.json()["id"], "surgery_name": "甲院申请的手术",
                             "surgery_level": "three", "planned_date": "2026-04-01"})
    assert surg.status_code == 201, surg.text

    hand = client.post("/api/inpatient/handovers", headers=clin["doc_a"],
                       json={"ward_id": clin["ward"]["id"], "shift": "day",
                             "handover_date": "2026-04-02", "handover_from": "甲院医师甲",
                             "handover_to": "甲院医师乙", "content": "甲院写的交接班"})
    assert hand.status_code == 201, hand.text


def test_全域角色跨机构照旧放行_这是设计不是洞(client, clin):
    """县级统筹本就跨机构（`GLOBAL_ROLES`），不该跟着一起关掉。"""
    adm = _admit(client, clin)   # 这一句本身就是 admin 跨机构办的入院
    resp = client.post("/api/inpatient/orders", headers=clin["admin"],
                       json={"admission_id": adm["id"], "order_type": "temp",
                             "content": "统筹角色开的临时医嘱", "frequency": "st"})
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------- 判定留痕


def test_四条只有全域角色够得着的端点不该被当成洞(client):
    """`create_slot` / `create_bed`（require_admin）与两条 cost（director）。

    它们的角色门只允许 `GLOBAL_ROLES`，**非全域角色连角色门都过不去**，
    所以不是可越权入口。把判定钉成用例，免得下一轮扫描又把它们当欠账去"修"——
    那会写出四条触发不了的"漏洞修复"。判据：这些端点的装饰器里确实只出现
    全域角色（或 require_admin）。
    """
    import ast
    import pathlib

    targets = {
        "routers/appointments.py": "create_slot",
        "routers/inpatient.py": "create_bed",
        "routers/cost.py": "upsert_department_cost",
        "routers/cost.py:2": "create_allocation_rule",
    }
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    for key, fn_name in targets.items():
        rel = key.split(":")[0]
        tree = ast.parse((app_dir / rel).read_text(encoding="utf-8"))
        consts = _module_role_consts(tree)
        found = False
        for node in ast.walk(tree):
            if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == fn_name):
                continue
            found = True
            admin_only, names = _role_gate(node, consts)
            assert admin_only or names <= {"admin", "director"}, (
                f"{rel}:{fn_name} 的角色门已经放宽到非全域角色 {sorted(names)}，"
                "它现在**是**可越权入口了，请补归属校验并从这条用例里移走"
            )
        assert found, f"{rel} 里找不到 {fn_name}——判定的前提已经变了"


def _module_role_consts(tree) -> dict[str, list[str]]:
    """模块级的角色常量（如 `SERVICE_ROLES = ("doctor", "public_health", "director")`）。"""
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, (ast.Tuple, ast.List, ast.Set))):
            names = [e.value for e in node.value.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if names:
                out[node.targets[0].id] = names
    return out


def _role_gate(fn, consts: dict[str, list[str]]) -> tuple[bool, set[str]]:
    """(是不是 require_admin, 角色名集合)。

    **必须认得 `require_roles(*SERVICE_ROLES)` 这种星号展开**——本文件初稿用的是
    `re.findall(r"'([a-z_]+)'", ...)`，遇到星号参数会得到**空集合**，而空集合
    `<= {"admin","director"}` 恒真，于是这条用例会**为错误的理由通过**。
    2026-09-18 就是这么把 `spd/tasks.py:start_path_instance` 误判成"连角色门都没有"的
    （它其实是 `require_roles(*SERVICE_ROLES)` = doctor/public_health/director）。
    解析不出来的参数一律留成 `<未解析:…>` 字符串，它不在全域角色集合里，
    会让断言红掉——**宁可误红，不可空转**。
    """
    admin_only, roles = False, set()
    for dec in fn.decorator_list:
        for sub in ast.walk(dec):
            if isinstance(sub, ast.Name) and sub.id == "require_admin":
                admin_only = True
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "require_roles"):
                for arg in sub.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        roles.add(arg.value)
                    elif isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Name):
                        resolved = consts.get(arg.value.id)
                        roles.update(resolved if resolved
                                     else [f"<未解析:{arg.value.id}>"])
                    else:
                        roles.add(f"<未解析:{ast.unparse(arg)}>")
    return admin_only, roles
