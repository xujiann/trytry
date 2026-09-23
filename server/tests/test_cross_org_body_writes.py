"""以别家机构的名义写：十条实测行为回归（P1-39 的现场）。

`tests/test_body_org_write_guard.py` 是静态闸门，守的是「每个入参带机构标识的
写接口都调了 `assert_org_writable`」。本文件守的是**它真的拦得住**——静态守卫
只能证明那行代码在，证明不了它挡住了什么。

这十条都是**先复现、后修复**的：补校验之前逐条实打过，12 个探针里 9 个返回
201/200，乙卫生院的医师以甲县医院的名义开了处方、报了传染病卡、开了检查单、
上转了患者；乙院的经办从甲院药房调出了药品、把甲院的职工派驻了出去。

后果分三档，都不轻：
* **记到别家账上**——就诊、家医签约、传染病报卡：监管报数按机构统计，记错家
  等于同时虚增一家、虚减一家，而且事后看不出来；
* **以别家名义对外发起**——转诊、会诊、检查申请：接收方看到的是甲院开的单；
* **动别家的实物与人**——药品调拨、人员派驻：这两条动的是真东西。
"""
import pytest
from conftest import reset_database
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def env(client):
    adm = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]}
    a = client.post("/api/organizations", json={
        "name": "越权甲县医院", "org_type": "lead_hospital", "level": "county"}, headers=adm).json()
    b = client.post("/api/organizations", json={
        "name": "越权乙卫生院", "org_type": "township", "level": "township"}, headers=adm).json()
    # 第三家：派驻"谎报派出方"那条要一个既非甲院、也非乙院的接收方——
    # 若接收方就是乙院，旧代码会先以"派出与接收机构不能相同"422 掉，
    # 探针在变异验证时红得不对（它根本没复现那个洞）。实测踩过。
    c = client.post("/api/organizations", json={
        "name": "越权丙卫生站", "org_type": "township", "level": "township"}, headers=adm).json()
    pt = client.post("/api/patients", json={
        "name": "越权探针患者", "id_card": "330102199202021234"}, headers=adm).json()
    client.post("/api/dictionaries", json={
        "category": "drug", "code": "XQ001", "name": "越权探针药"}, headers=adm)
    client.post("/api/pharmacy/stocks", json={
        "org_id": a["id"], "drug_code": "XQ001", "drug_name": "越权探针药",
        "quantity": 500}, headers=adm)
    emp = client.post("/api/mgmt/employees", json={
        "org_id": a["id"], "name": "甲院职工", "title": "主治医师"}, headers=adm)
    # P1-51 的两个探针各用一个甲院职工：若某条放行，员工被翻成 seconded，
    # 共用同一人会让下一条变成 409 而不是 403，报错指向错的原因。
    emp_legacy, emp_forged = (
        client.post("/api/mgmt/employees", json={
            "org_id": a["id"], "name": name, "title": "主治医师"}, headers=adm).json()
        for name in ("甲院职工-旧入口探针", "甲院职工-谎报派出方探针")
    )

    def user(name, role, org):
        client.post("/api/users", json={
            "username": name, "password": "Xquan#2026x", "role": role,
            "org_id": org["id"]}, headers=adm)
        return {"Authorization": "Bearer " + client.post(
            "/api/auth/login", json={"username": name, "password": "Xquan#2026x"}
        ).json()["access_token"]}

    return {
        "adm": adm, "A": a, "B": b, "C": c, "pt": pt,
        "emp": emp.json() if emp.status_code in (200, 201) else {},
        "emp_legacy": emp_legacy, "emp_forged": emp_forged,
        "doctor_b": user("xq_doc_b", "doctor", b),
        "operator_b": user("xq_op_b", "operator", b),
        "operator_a": user("xq_op_a", "operator", a),
    }


def _cases(env):
    A, pt = env["A"], env["pt"]
    rows = [
        ("就诊：在甲院开就诊", "/api/encounters", "doctor_b",
         {"patient_id": pt["id"], "org_id": A["id"], "visit_type": "outpatient",
          "visit_date": "2026-09-19"}),
        ("处方：以甲院名义开方", "/api/prescriptions", "doctor_b",
         {"patient_id": pt["id"], "org_id": A["id"],
          "items": [{"drug_code": "XQ001", "drug_name": "越权探针药", "qty": 1,
                     "usage": "口服", "daily_dose": 1.0}]}),
        ("传染病：报卡记到甲院头上", "/api/infectious/cases", "doctor_b",
         {"patient_id": pt["id"], "org_id": A["id"], "disease_code": "A01",
          "disease_name": "霍乱", "report_date": "2026-09-19", "onset_date": "2026-09-18"}),
        ("家医签约：替甲院签", "/api/contracts", "doctor_b",
         {"patient_id": pt["id"], "org_id": A["id"], "doctor_name": "探针医生",
          "start_date": "2026-09-19"}),
        ("检查申请：以甲院名义开单", "/api/exams", "doctor_b",
         {"patient_id": pt["id"], "from_org_id": A["id"], "center_type": "imaging",
          "item_code": "E1", "item_name": "胸片"}),
        ("转诊：以甲院名义上转", "/api/referrals", "doctor_b",
         {"patient_id": pt["id"], "from_org_id": A["id"], "to_org_id": env["B"]["id"],
          "reason": "探针", "direction": "up"}),
        ("会诊：以甲院名义申请", "/api/consultations", "doctor_b",
         {"patient_id": pt["id"], "from_org_id": A["id"], "to_org_id": env["B"]["id"],
          "purpose": "探针", "question": "探针问题"}),
        ("调拨：从甲院药房调出", "/api/pharmacy/transfers", "operator_b",
         {"from_org_id": A["id"], "to_org_id": env["B"]["id"],
          "drug_code": "XQ001", "quantity": 1}),
    ]
    if env["emp"].get("id"):
        rows.append(("派驻：把甲院的职工派出去", "/api/staffing/secondments", "operator_b",
                     {"employee_id": env["emp"]["id"], "from_org_id": A["id"],
                      "to_org_id": env["B"]["id"], "start_date": "2026-09-19"}))
    # P1-51：上面那条只证明了"照实填 from_org=甲院会被拦"。下面两条是 P1-39 漏掉的：
    rows += [
        # ① 同一张表上的**第二个**建派驻入口：原先一道归属校验都没有，实测 201
        ("派驻（旧入口 /api/mgmt）：把甲院的职工派出去", "/api/mgmt/secondments",
         "operator_b",
         {"employee_id": env["emp_legacy"]["id"], "to_org_id": env["B"]["id"],
          "start_date": "2026-09-19"}),
        # ② 新入口，但把 from_org_id **谎报成自己院**：原先校验的是这个自报字段，
        #    于是照样 201，甲院的人被派走、台账上还记着一个假的派出机构
        ("派驻（谎报派出方=乙院）：把甲院的职工派出去", "/api/staffing/secondments",
         "operator_b",
         {"employee_id": env["emp_forged"]["id"], "from_org_id": env["B"]["id"],
          "to_org_id": env["C"]["id"], "start_date": "2026-09-19",
          "assignment_type": "support"}),
    ]
    return rows


def test_乙院用户不得以甲院名义写(client, env):
    """把各自的 `assert_org_writable` 去掉，对应那条必红——补校验之前实测全是 201。"""
    passed_through = []
    for label, path, role, body in _cases(env):
        r = client.post(path, json=body, headers=env[role])
        if r.status_code in (200, 201):
            passed_through.append(f"{label} → {r.status_code}")
        else:
            assert r.status_code == 403, (
                f"{label} 期望 403（无权以该机构名义写入），实际 {r.status_code}：{r.text[:160]}"
            )
            assert "无权以该机构名义写入" in r.json().get("detail", ""), (
                f"{label} 被拒了，但不是因为归属校验：{r.text[:160]}"
            )
    assert passed_through == [], (
        "以下写接口放行了「以别家机构名义写」：\n  " + "\n  ".join(passed_through)
    )


def test_本机构照常写得进(client, env):
    """守卫不能误伤本职工作：同一个用户往自己机构写必须照常成功。"""
    B = env["B"]
    ok = client.post("/api/encounters", json={
        "patient_id": env["pt"]["id"], "org_id": B["id"],
        "visit_type": "outpatient", "visit_date": "2026-09-19"},
        headers=env["doctor_b"])
    assert ok.status_code == 201, ok.text
    assert ok.json()["org_id"] == B["id"]


def test_全域角色跨机构照常(client, env):
    """admin/director 是全域角色，跨机构写是它们的正常职权，不该被这道守卫挡住。"""
    ok = client.post("/api/encounters", json={
        "patient_id": env["pt"]["id"], "org_id": env["A"]["id"],
        "visit_type": "outpatient", "visit_date": "2026-09-19"},
        headers=env["adm"])
    assert ok.status_code == 201, ok.text


def test_派出机构必须是员工所在机构(client, env):
    """本院经办填错派出方（写成别的院）→ 422，不许把假派出方记进台账。

    这是和越权分开的一件事：甲院经办派甲院的人，有权；但 `from_org_id` 若不是
    该员工所在机构，台账就记了一条假的派出记录——监测指标按派出机构统计，
    会平白算到别家头上。原先只要调用方对自报的那个机构有写权限就放行。
    """
    emp = client.post("/api/mgmt/employees", json={
        "org_id": env["A"]["id"], "name": "甲院职工-填错派出方", "title": "主治医师"},
        headers=env["adm"]).json()
    r = client.post("/api/staffing/secondments", json={
        "employee_id": emp["id"], "from_org_id": env["B"]["id"],
        "to_org_id": env["C"]["id"], "start_date": "2026-09-19",
        "assignment_type": "support"}, headers=env["adm"])
    assert r.status_code == 422, r.text
    assert "派出机构须是该员工所在机构" in r.json()["detail"]


def test_本院经办照常派本院的人(client, env):
    """守卫不能误伤本职：甲院经办照实派甲院职工，照常 201 且派出方记对。"""
    emp = client.post("/api/mgmt/employees", json={
        "org_id": env["A"]["id"], "name": "甲院职工-正常派驻", "title": "主治医师"},
        headers=env["adm"]).json()
    r = client.post("/api/staffing/secondments", json={
        "employee_id": emp["id"], "from_org_id": env["A"]["id"],
        "to_org_id": env["B"]["id"], "start_date": "2026-09-19",
        "assignment_type": "support"}, headers=env["operator_a"])
    assert r.status_code == 201, r.text
    assert r.json()["from_org_id"] == env["A"]["id"]
    assert r.json()["assignment_type"] == "support"


# ---------------------------------------------------------------------------
# P1-56：按 id 引用别家的实体——请求体里自报的机构对得上，真正被写的却是别家
# ---------------------------------------------------------------------------
#
# 上面那十条都是"以别家机构的名义写"：请求体里的机构字段填的就是别家。
# 下面这一批更隐蔽：机构字段要么没有、要么**照实填成自己院**（校验当然过），
# 但请求体里还带着一个 id——别家的疫苗批次、住院单、手术室、纳管档案——
# 真正被写的是那个实体所属的机构。建闸门时逐条实打：**全部放行**（201/200）。
# 其中最重的几条动的是钱与实物：从甲院住院单**退押金**、把甲院的住院单**结算**掉
# （结算单记在甲院名下）、**订甲院的手术室**、用甲院的**疫苗批次**接种（扣甲院库存）。


@pytest.fixture(scope="module")
def a_side(client, env):
    """甲院这边的实体：乙院账号要去碰的东西。"""
    from app.database import SessionLocal
    from app.models import (
        Admission, Bed, BillDetail, ChargeItem, Department, Deposit, Employee, Encounter,
        OperatingRoom, SurgeryRequest, VaccineBatch, Ward,
    )
    from app.spd.models import SpdCandidate, SpdEnrollment, SpdPathTemplate, SpdProgram, SpdTask

    A, B, adm = env["A"]["id"], env["B"]["id"], env["adm"]
    pt = client.post("/api/patients", json={
        "name": "P1-56 甲院住院患者", "id_card": "330102199303031234"}, headers=adm).json()["id"]
    pt_free = client.post("/api/patients", json={
        "name": "P1-56 未住院患者", "id_card": "330102199303035678"}, headers=adm).json()["id"]
    with SessionLocal() as db:
        ward_a, ward_b = Ward(org_id=A, name="甲院病区"), Ward(org_id=B, name="乙院病区")
        db.add_all([ward_a, ward_b]); db.flush()
        bed_a, bed_a2, bed_b = (Bed(ward_id=ward_a.id, bed_no="A1"), Bed(ward_id=ward_a.id, bed_no="A2"),
                                Bed(ward_id=ward_b.id, bed_no="B1"))
        db.add_all([bed_a, bed_a2, bed_b]); db.flush()
        adm_a = Admission(patient_id=pt, org_id=A, ward_id=ward_a.id, bed_id=bed_a.id,
                          created_by=1, status="admitted")
        adm_b = Admission(patient_id=pt, org_id=B, ward_id=ward_b.id, bed_id=bed_b.id,
                          created_by=1, status="admitted")
        db.add_all([adm_a, adm_b]); db.flush()
        enc_a = Encounter(patient_id=pt, org_id=A)
        item = db.query(ChargeItem).filter(ChargeItem.active.is_(True)).first()
        if item is None:
            item = ChargeItem(code="P156", name="P1-56 探针项目", price=10)
            db.add(item); db.flush()
        db.add_all([
            enc_a,
            Deposit(admission_id=adm_a.id, amount=1000, deposit_type="prepay",
                    method="cash", operator="甲院收费"),
            BillDetail(patient_id=pt, admission_id=adm_a.id, item_code=item.code,
                       item_name=item.name, unit_price=10, quantity=1, amount=10, created_by=1),
        ])
        emp_a = Employee(org_id=A, name="甲院护士")
        dept_a = Department(org_id=A, code="P156", name="甲院内科")
        room_a = OperatingRoom(org_id=A, name="甲院1号手术室")
        sr_b = SurgeryRequest(admission_id=adm_b.id, patient_id=pt, org_id=B,
                              surgery_name="乙院的手术", created_by=1, status="approved")
        batch_a = VaccineBatch(org_id=A, vaccine_code="HepB", vaccine_name="乙肝疫苗",
                               batch_no="A-001", manufacturer="x", expire_date="2030-01-01",
                               quantity=10)
        prog = SpdProgram(code="p156", name="P1-56 病种")
        db.add_all([emp_a, dept_a, room_a, sr_b, batch_a, prog]); db.flush()
        # 必须是**已发布**的模板：草稿会先以"只能引用已发布的路径模板"422 掉，
        # 那样这条探针在修复前也红不了（实测踩过——它当时是 19 条里唯一没报 201 的）
        tpl = SpdPathTemplate(program_id=prog.id, code="p156", name="P1-56 路径",
                              status="published")
        enr_a = SpdEnrollment(patient_id=pt, program_code="p156", org_id=A)
        cand_a = SpdCandidate(patient_id=pt, program_code="p156", org_id=A, status="suspect")
        task_a = SpdTask(patient_id=pt, title="甲院的任务", org_id=A, status="pending")
        db.add_all([tpl, enr_a, cand_a, task_a]); db.flush()
        # 还得有至少一个节点——否则先以"路径模板没有节点"422。这条探针两次红得不对
        # （草稿模板、空模板），每次都是被前一道业务校验挡住、根本没走到归属校验
        from app.spd.models import SpdPathNode
        db.add(SpdPathNode(template_id=tpl.id, key="first", name="首诊评估"))
        db.commit()
        from app.models import User
        user_a = db.query(User).filter(User.username == "xq_op_a").one()
        return {k: v.id if hasattr(v, "id") else v for k, v in dict(
            pt=pt, pt_free=pt_free, ward_a=ward_a, bed_a2=bed_a2, adm_a=adm_a, enc_a=enc_a,
            emp_a=emp_a, dept_a=dept_a, room_a=room_a, sr_b=sr_b, batch_a=batch_a, tpl=tpl,
            enr_a=enr_a, cand_a=cand_a, task_a=task_a, user_a=user_a,
            item=item.code).items()}


def _p56_cases(env, a):
    B = env["B"]["id"]
    return [
        ("接种：用甲院的疫苗批次", "doctor_b", "/api/vaccination/records",
         {"patient_id": a["pt"], "org_id": B, "vaccine_code": "HepB", "vaccine_name": "乙肝疫苗",
          "dose_no": 1, "batch_id": a["batch_a"]}),
        ("采购：挂到甲院的科室", "operator_b", "/api/materials/purchases",
         {"org_id": B, "dept_id": a["dept_a"], "item_name": "口罩"}),
        ("住院：收进甲院病区", "doctor_b", "/api/inpatient/admissions",
         {"patient_id": a["pt_free"], "ward_id": a["ward_a"], "bed_id": a["bed_a2"]}),
        ("医嘱：给甲院住院患者下", "doctor_b", "/api/inpatient/orders",
         {"admission_id": a["adm_a"], "order_type": "temp", "content": "探针"}),
        ("押金：交到甲院住院单", "operator_b", "/api/billing/deposits",
         {"admission_id": a["adm_a"], "amount": 1}),
        ("退押金：从甲院住院单退钱", "operator_b", "/api/billing/deposits/refund",
         {"admission_id": a["adm_a"], "amount": 100}),
        ("计费：记到甲院住院单", "doctor_b", "/api/billing/details",
         {"patient_id": a["pt"], "admission_id": a["adm_a"], "item_code": a["item"], "quantity": 1}),
        ("结算：把甲院住院单结掉", "operator_b", "/api/billing/settlements",
         {"bill_type": "inpatient", "admission_id": a["adm_a"]}),
        ("交班：写进甲院病区", "doctor_b", "/api/inpatient/handovers",
         {"ward_id": a["ward_a"], "shift": "day", "handover_date": "2026-09-20"}),
        ("病历：写甲院的就诊", "doctor_b", "/api/quality/records",
         {"encounter_id": a["enc_a"], "chief_complaint": "探针"}),
        ("手术申请：挂甲院住院单", "doctor_b", "/api/surgery/requests",
         {"admission_id": a["adm_a"], "surgery_name": "探针手术"}),
        ("排台：订甲院的手术室", "operator_b", f"/api/surgery/requests/{a['sr_b']}/schedule",
         {"room_id": a["room_a"], "scheduled_date": "2026-09-25",
          "start_time": "08:00", "end_time": "10:00"}),
        ("合同：给甲院员工签", "operator_b", "/api/mgmt/staff-contracts",
         {"employee_id": a["emp_a"], "contract_no": "P156-1",
          "start_date": "2026-01-01", "end_date": "2027-01-01"}),
        ("目标池：不给 org_id 就改甲院的候选人", "doctor_b", "/api/spd/candidates/distribute",
         {"candidate_ids": [a["cand_a"]]}),
        ("目标池：把甲院的候选人划进乙院", "doctor_b", "/api/spd/candidates/distribute",
         {"candidate_ids": [a["cand_a"]], "org_id": B}),
        ("路径：在甲院的纳管档案上启动", "doctor_b", "/api/spd/path-instances",
         {"enrollment_id": a["enr_a"], "template_id": a["tpl"]}),
        ("任务：挂甲院的纳管档案（派进甲院队列）", "doctor_b", "/api/spd/tasks",
         {"patient_id": a["pt"], "title": "探针", "enrollment_id": a["enr_a"], "org_id": B}),
        ("病程：写进甲院住院病历", "doctor_b", f"/api/inpatient/admissions/{a['adm_a']}/progress-notes",
         {"note_type": "daily", "content": "探针"}),
        ("护理：写进甲院住院病历", "operator_b", f"/api/inpatient/admissions/{a['adm_a']}/nursing-records",
         {"nursing_level": "level2", "content": "探针"}),
        ("体征：录进甲院住院病历", "operator_b", f"/api/inpatient/admissions/{a['adm_a']}/vitals",
         {"measured_at": "2026-09-20 08:00", "temperature": 36.5}),
        ("村医：把甲院的用户建成乙院村医", "doctor_b", "/api/spd/village-doctors",
         {"user_id": a["user_a"], "org_id": B}),
        # 单条任务接口：批量接口已跳过别家的任务，单条的若放行，批量那道门一绕就过
        ("任务：单条认领甲院的任务", "doctor_b", f"/api/spd/tasks/{a['task_a']}/claim", None),
        ("任务：单条催办甲院的任务", "doctor_b", f"/api/spd/tasks/{a['task_a']}/urge", None),
        ("任务：单条升级甲院的任务", "doctor_b", f"/api/spd/tasks/{a['task_a']}/escalate", None),
    ]


def test_按id引用别家实体的写入被拦(client, env, a_side):
    """建闸门时这 24 条逐条实打全部放行；把各自那一行校验删掉，对应那条必红。"""
    passed_through = []
    for label, role, path, body in _p56_cases(env, a_side):
        r = client.post(path, json=body, headers=env[role])
        if r.status_code in (200, 201):
            passed_through.append(f"{label} → {r.status_code}")
        else:
            assert r.status_code in (403, 422), (
                f"{label} 期望 403/422（归属不符），实际 {r.status_code}：{r.text[:160]}"
            )
    assert passed_through == [], "以下写入动到了别家的实体：\n  " + "\n  ".join(passed_through)


def test_批量处理任务跳过别家的而不是整批失败(client, env, a_side):
    """`/tasks/batch` 的口径是逐条判定、不满足的跳过并说明——别家的任务也走这条路。"""
    r = client.post("/api/spd/tasks/batch",
                    json={"task_ids": [a_side["task_a"]], "action": "cancel"},
                    headers=env["doctor_b"])
    assert r.status_code == 200, r.text
    assert r.json()["processed"] == 0
    assert r.json()["skipped"] == [{"id": a_side["task_a"], "reason": "无权处理其他机构的任务"}]


def test_甲院押金与候选人没被动过(client, env, a_side):
    """403 之前是否已经写进去了一半——看数据本身，不只看状态码。

    自己发这两次请求再看数据，不依赖上一条用例先跑过（P1-55：否则本条若先跑，
    什么都还没发生，它就会为了错的理由而绿）。
    """
    from app.database import SessionLocal
    from app.models import Deposit
    from app.spd.models import SpdCandidate

    client.post("/api/billing/deposits/refund",
                json={"admission_id": a_side["adm_a"], "amount": 100}, headers=env["operator_b"])
    client.post("/api/spd/candidates/distribute",
                json={"candidate_ids": [a_side["cand_a"]], "org_id": env["B"]["id"]},
                headers=env["doctor_b"])
    with SessionLocal() as db:
        refunds = db.query(Deposit).filter(
            Deposit.admission_id == a_side["adm_a"], Deposit.deposit_type == "refund").count()
        cand = db.get(SpdCandidate, a_side["cand_a"])
    assert refunds == 0, "甲院住院单上出现了退押金流水"
    assert cand.org_id == env["A"]["id"], "甲院的候选人被划到了别家"
    assert cand.team_id is None and cand.assigned_user_id is None


# ---------------------------------------------------------------------------
# P1-57：按**路径** id 经 helper 取实体——两道横向闸门都看不见的那一形
# ---------------------------------------------------------------------------
#
# 按 id 写闸门（test_stage15_horizontal）认的是 handler 源码里出现
# `db.get(带 org_id 的模型,`；取数写进 `_get` / `_close` / `_pending` 之类的 helper，
# 它就看不见。P1-56 的新判据只管请求体里的 id。于是这一批两边都漏了，
# 逐条实打全部放行——包括替别家的**知情同意书**登记签字，以及与一张会诊单毫不相干
# 的**第三家**机构受理、拒绝它。
#
# 每条探针都先把实体推到"业务上允许这个动作"的状态，确保它是被**归属校验**拦下的，
# 而不是被前面某道业务校验先拒了（P1-56 那条"路径启动"探针两次红得不对，就是这么来的）。


@pytest.fixture(scope="module")
def p57(client, env):
    from app.database import SessionLocal
    from app.models import (
        AdminProject, Consultation, DiseaseEnrollment, DiseaseProgram, InformedConsent,
        Resource, VisitCredential,
    )
    from app.spd.models import SpdEnrollment, SpdPathInstance, SpdPathTemplate, SpdProgram

    A, B = env["A"]["id"], env["B"]["id"]
    pt = client.post("/api/patients", json={
        "name": "P1-57 探针患者", "id_card": "330102199606061234"}, headers=env["adm"]).json()["id"]
    with SessionLocal() as db:
        # 甲院向乙院申请的会诊，分别停在各动作允许的状态
        def cons(status):
            return Consultation(patient_id=pt, from_org_id=A, to_org_id=B, question="探针",
                                created_by=1, status=status)
        c_applied, c_applied2, c_accepted, c_done, c_done2 = (
            cons("applied"), cons("applied"), cons("accepted"), cons("completed"), cons("completed"))
        creds = [VisitCredential(patient_id=pt, credential_no=f"P157-{i}", org_id=A) for i in range(2)]
        dp = DiseaseProgram(code="p157", name="P1-57 专病", org_id=A,
                            path_nodes=[{"key": "n1", "name": "首诊"}])
        db.add(dp); db.flush()
        denr = [DiseaseEnrollment(program_id=dp.id, patient_id=pt, org_id=A) for _ in range(2)]
        consents = [InformedConsent(patient_id=pt, org_id=A, consent_type="surgery",
                                    title=f"同意书{i}", created_by=1) for i in range(2)]
        proj = AdminProject(org_id=A, name="甲院项目")
        res = [Resource(org_id=A, resource_type="ct", code=f"P157CT{i}", name=f"甲院CT{i}",
                        status=st) for i, st in enumerate(("draft", "draft", "published"))]
        prog = SpdProgram(code="p157", name="P1-57 病种")
        db.add(prog); db.flush()
        tpl = SpdPathTemplate(program_id=prog.id, code="p157", name="路径", status="published")
        senr = SpdEnrollment(patient_id=pt, program_code="p157", org_id=A)
        db.add_all([c_applied, c_applied2, c_accepted, c_done, c_done2, *creds, *denr, *consents,
                    proj, *res, tpl, senr]); db.flush()
        inst = SpdPathInstance(enrollment_id=senr.id, template_id=tpl.id, owner_user_id=1)
        db.add(inst); db.commit()
        return {
            "c_applied": c_applied.id, "c_applied2": c_applied2.id, "c_accepted": c_accepted.id,
            "c_done": c_done.id, "c_done2": c_done2.id,
            "creds": [x.id for x in creds], "denr": [x.id for x in denr],
            "consents": [x.id for x in consents], "proj": proj.id,
            "res": [x.id for x in res], "inst": inst.id,
        }


@pytest.fixture(scope="module")
def third_party(client, env):
    """丙院的医师与经办：与这些会诊单毫不相干的第三家。"""
    out = {}
    for name, role in (("xq_doc_c", "doctor"), ("xq_op_c", "operator")):
        client.post("/api/users", json={"username": name, "password": "Xquan#2026x",
                                        "role": role, "org_id": env["C"]["id"]}, headers=env["adm"])
        out[role] = {"Authorization": "Bearer " + client.post(
            "/api/auth/login", json={"username": name, "password": "Xquan#2026x"}
        ).json()["access_token"]}
    return out


def _p57_cases(env, t, x):
    return [
        ("会诊：第三家受理", t["doctor"], "post", f"/api/consultations/{x['c_applied']}/accept",
         {"expert_name": "丙院张"}),
        ("会诊：第三家拒绝", t["doctor"], "post", f"/api/consultations/{x['c_applied2']}/decline", None),
        ("会诊：第三家出具意见", t["doctor"], "post", f"/api/consultations/{x['c_accepted']}/complete",
         {"opinion": "探针"}),
        ("会诊：第三家计费", t["operator"], "post", f"/api/consultations/{x['c_done']}/fee", {"fee": 100}),
        ("会诊：第三家评价", t["doctor"], "post", f"/api/consultations/{x['c_done2']}/rate", {"rating": 1}),
        ("凭据：回收甲院的就诊凭据", env["operator_b"], "post",
         f"/api/credentials/{x['creds'][0]}/recycle", {}),
        ("凭据：作废甲院的就诊凭据", env["operator_b"], "post",
         f"/api/credentials/{x['creds'][1]}/void", {"reason": "探针"}),
        ("专病：给甲院的入组记节点", env["doctor_b"], "post",
         f"/api/disease-programs/enrollments/{x['denr'][0]}/records", {"node_key": "n1"}),
        ("专病：让甲院的入组出组", env["doctor_b"], "post",
         f"/api/disease-programs/enrollments/{x['denr'][1]}/exit", {}),
        ("同意书：替甲院的同意书登记签字", env["doctor_b"], "post",
         f"/api/outpatient/consents/{x['consents'][0]}/sign", {"signer_name": "探针"}),
        ("同意书：替甲院的同意书登记拒签", env["doctor_b"], "post",
         f"/api/outpatient/consents/{x['consents'][1]}/refuse",
         {"signer_name": "探针", "refuse_reason": "探针"}),
        ("项目：改甲院的项目", env["operator_b"], "patch", f"/api/projects/{x['proj']}",
         {"progress_pct": 99}),
        ("项目：给甲院的项目加里程碑", env["operator_b"], "post",
         f"/api/projects/{x['proj']}/milestones", {"name": "探针"}),
        ("资源：改甲院的共享资源", env["operator_b"], "patch", f"/api/resources/{x['res'][0]}",
         {"name": "被改了"}),
        ("资源：把甲院的资源发布进共享池", env["operator_b"], "post",
         f"/api/resources/{x['res'][1]}/publish", None),
        ("资源：撤下甲院已发布的资源", env["operator_b"], "post",
         f"/api/resources/{x['res'][2]}/withdraw", {"reason": "探针"}),
        ("路径：调整甲院患者的路径实例", env["doctor_b"], "patch",
         f"/api/spd/path-instances/{x['inst']}", {"status": "paused"}),
    ]


def test_经helper按路径id取的别家实体写入被拦(client, env, p57, third_party):
    """建闸门时这 17 条逐条实打全部放行；删掉对应那一行校验，对应那条必红。"""
    passed_through = []
    for label, who, method, path, body in _p57_cases(env, third_party, p57):
        r = getattr(client, method)(path, json=body, headers=who)
        if r.status_code in (200, 201):
            passed_through.append(f"{label} → {r.status_code}")
        else:
            assert r.status_code == 403, f"{label} 期望 403，实际 {r.status_code}：{r.text[:160]}"
    assert passed_through == [], "以下写入动到了别家的实体：\n  " + "\n  ".join(passed_through)


def test_会诊双方各做各的动作(client, env, p57):
    """跨机构是会诊本身——拦第三方不能顺手把**双方**的正当动作也拦了。

    受邀方（乙院）受理 → 200；申请方（甲院）不能替对方受理 → 403；
    申请方评价 → 200，受邀方给自己评分 → 403。
    """
    from app.database import SessionLocal
    from app.models import Consultation

    A, B = env["A"]["id"], env["B"]["id"]
    with SessionLocal() as db:
        fresh = Consultation(patient_id=1, from_org_id=A, to_org_id=B, question="双方",
                             created_by=1, status="applied")
        rated = Consultation(patient_id=1, from_org_id=A, to_org_id=B, question="评价",
                             created_by=1, status="completed")
        db.add_all([fresh, rated]); db.commit()
        fresh_id, rated_id = fresh.id, rated.id
    # 甲院医师（申请方）
    client.post("/api/users", json={"username": "xq_doc_a57", "password": "Xquan#2026x",
                                    "role": "doctor", "org_id": A}, headers=env["adm"])
    doc_a = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "xq_doc_a57", "password": "Xquan#2026x"}
    ).json()["access_token"]}

    assert client.post(f"/api/consultations/{fresh_id}/accept", json={"expert_name": "甲"},
                       headers=doc_a).status_code == 403, "申请方不能替受邀方受理"
    assert client.post(f"/api/consultations/{fresh_id}/accept", json={"expert_name": "乙院王"},
                       headers=env["doctor_b"]).status_code == 200, "受邀方受理应照常"
    assert client.post(f"/api/consultations/{rated_id}/rate", json={"rating": 5},
                       headers=env["doctor_b"]).status_code == 403, "受邀方不能给自己评分"
    assert client.post(f"/api/consultations/{rated_id}/rate", json={"rating": 5},
                       headers=doc_a).status_code == 200, "申请方评价应照常"


# ---- P1-57 第二批：闸门认 `*org_id` 列之后才看见的 ----
#
# 这些实体的机构列都不叫 `org_id`（`managed_by_org_id` / `center_org_id` / `dest_org_id` /
# `claimed_org_id` / `lead_org_id`，或 `from_org_id`+`to_org_id` 双方），原闸门按字面
# `org_id` 找模型，整族不在视野里。同样每条先把实体推到"业务上允许这个动作"的状态。


@pytest.fixture(scope="module")
def p57b(client, env):
    from app.database import SessionLocal
    from app.models import (
        ArchiveAuthorization, ChronicPatient, EmergencyCase, ExamRequest, Referral,
        SterilizationBatch,
    )
    from app.spd.models import SpdCenter, SpdProgram

    A, B = env["A"]["id"], env["B"]["id"]
    pt = client.post("/api/patients", json={
        "name": "P1-57 第二批探针患者", "id_card": "330102199707071234"}, headers=env["adm"]).json()["id"]
    with SessionLocal() as db:
        chronic = ChronicPatient(patient_id=pt, disease="hypertension", managed_by_org_id=A)
        batch = SterilizationBatch(batch_no="P157B-1", center_org_id=A, item_name="探针器械",
                                   quantity=1)
        case = EmergencyCase(location="探针路口", dest_org_id=A, status="arrived")
        # 乙院申请、已被甲院（诊断中心）领取的单子；另一张没人领的
        def exam(**kw):
            return ExamRequest(patient_id=pt, from_org_id=B, center_type="imaging",
                               item_code="CT01", item_name="头颅CT", created_by=1, **kw)
        claimed = exam(status="diagnosing", claimed_org_id=A, claimed_by="甲院中心")
        unclaimed = exam(status="pending")
        claimed_ok = exam(status="diagnosing", claimed_org_id=A, claimed_by="甲院中心")
        referrals = [Referral(patient_id=pt, from_org_id=A, to_org_id=B, direction="up",
                              status="accepted", created_by=1) for _ in range(2)]
        prog = SpdProgram(code="p157b", name="P1-57 第二批病种", lead_org_id=A)
        center = SpdCenter(code="p157b", name="甲院牵头中心", program_code="p157b", lead_org_id=A)
        auth = ArchiveAuthorization(patient_id=pt, grantee_org_id=A, created_by=1)
        db.add_all([chronic, batch, case, claimed, unclaimed, claimed_ok, *referrals, prog, center,
                    auth])
        db.commit()
        return {
            "pt": pt, "chronic": chronic.id, "batch": batch.id, "case": case.id,
            "claimed": claimed.id, "unclaimed": unclaimed.id, "claimed_ok": claimed_ok.id,
            "referrals": [r.id for r in referrals], "prog": prog.id, "center": center.id,
            "auth": auth.id,
        }


def _p57b_cases(env, t, x):
    return [
        ("慢病：给甲院管理的慢病患者录随访", env["doctor_b"], "post",
         f"/api/chronic/{x['chronic']}/followups", {"sbp": 190, "dbp": 120}),
        ("消毒供应：推进甲院中心的灭菌批次", env["operator_b"], "post",
         f"/api/cssd/batches/{x['batch']}/advance", None),
        ("急救：替接收医院判定抢救转归", env["doctor_b"], "post",
         f"/api/emergency/cases/{x['case']}/rescue-outcome", {"rescue_outcome": "failed"}),
        ("诊断中心：给别家已领取的申请单出报告", t["doctor"], "post",
         f"/api/exams/{x['claimed']}/report", {"conclusion": "探针", "critical": True}),
        ("医保：给别家之间的转诊签发转诊证明", t["operator"], "post",
         f"/api/insurance/referral-certs/{x['referrals'][0]}", None),
        ("慢专病：改甲院牵头病种的纳管规则", env["doctor_b"], "patch",
         f"/api/spd/programs/{x['prog']}", {"description": "被改了"}),
        ("慢专病：给甲院牵头病种加管理目标", env["doctor_b"], "post",
         f"/api/spd/programs/{x['prog']}/targets", {"metric": "sbp", "target_high": 140}),
        ("慢专病：改甲院牵头的专病中心", env["doctor_b"], "patch",
         f"/api/spd/centers/{x['center']}", {"status": "stopped"}),
        ("慢专病：以甲院名义挂牌专病中心", env["doctor_b"], "post", "/api/spd/centers",
         {"code": "p157b-forged", "name": "冒名中心", "program_code": "p157b",
          "lead_org_id": env["A"]["id"]}),
    ]


def test_机构列不叫org_id的别家实体写入被拦(client, env, p57b, third_party):
    """建闸门时这 9 条逐条实打全部放行；删掉对应那一行校验，对应那条必红。"""
    passed_through = []
    for label, who, method, path, body in _p57b_cases(env, third_party, p57b):
        r = getattr(client, method)(path, json=body, headers=who)
        if r.status_code in (200, 201):
            passed_through.append(f"{label} → {r.status_code}")
        else:
            assert r.status_code == 403, f"{label} 期望 403，实际 {r.status_code}：{r.text[:160]}"
    assert passed_through == [], "以下写入动到了别家的实体：\n  " + "\n  ".join(passed_through)


def test_机构列不叫org_id的正当动作照常(client, env, p57b, third_party):
    """拦第三方不能顺手把正当的一方也拦了，也不能把"按设计不判"的那半边关掉。"""
    A = env["A"]["id"]
    client.post("/api/users", json={"username": "xq_doc_a57b", "password": "Xquan#2026x",
                                    "role": "doctor", "org_id": A}, headers=env["adm"])
    doc_a = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "xq_doc_a57b", "password": "Xquan#2026x"}
    ).json()["access_token"]}

    r = client.post(f"/api/exams/{p57b['claimed_ok']}/report", json={"conclusion": "正常"},
                    headers=doc_a)
    assert r.status_code == 201, f"领取了单子的中心出报告应照常：{r.text[:160]}"
    # 没人领过的单子不判归属：领取才是"哪家中心来诊断"的唯一落点
    r = client.post(f"/api/exams/{p57b['unclaimed']}/report", json={"conclusion": "正常"},
                    headers=third_party["doctor"])
    assert r.status_code == 201, f"未领取的单子行为不变：{r.text[:160]}"
    # 转诊双方任一可签（接收方乙院）
    r = client.post(f"/api/insurance/referral-certs/{p57b['referrals'][1]}",
                    headers=env["operator_b"])
    assert r.status_code == 200, f"转诊接收方签发应照常：{r.text[:160]}"
    r = client.post(f"/api/chronic/{p57b['chronic']}/followups", json={"sbp": 130, "dbp": 80},
                    headers=doc_a)
    assert r.status_code == 201, f"管理机构录随访应照常：{r.text[:160]}"


def test_撤销档案授权不拦但留痕(client, env, p57b):
    """撤销是患者的权利，在哪个窗口提出都得办得成——但谁替他撤的要查得到。"""
    from app.database import SessionLocal
    from app.models import AccessLog

    with SessionLocal() as db:
        before = db.query(AccessLog).filter(
            AccessLog.patient_id == p57b["pt"], AccessLog.resource == "authorization").count()
    r = client.post(f"/api/patients/{p57b['pt']}/authorizations/{p57b['auth']}/revoke",
                    headers=env["operator_b"])
    assert r.status_code == 200, "非被授权机构的窗口也得撤得成"
    with SessionLocal() as db:
        rows = db.query(AccessLog).filter(
            AccessLog.patient_id == p57b["pt"], AccessLog.resource == "authorization").all()
    assert len(rows) == before + 1, "撤销授权没有留痕"
    assert rows[-1].username == "xq_op_b" and rows[-1].basis == "consent_admin"


# ---- P1-58：归属在一跳之外，或单据上原本就缺处理方那一方 ----
#
# ① 一跳归属：里程碑之于项目、危急值报告之于申请单、服务包绑定之于纳管档案……
#    这些表自己没有机构列，闸门按"模型带不带 org_id"找，整族不在视野里；
# ② 缺处理方：急救事件没记调度方、中药订单没记承接药房、病理标本一个机构列都没有，
#    P1-57 只能按设计豁免——补列之后才判得了。
# 甲院是各实体的主人，乙院/丙院是来动它的人；每条探针先把实体推到业务允许该动作的状态。


def _login(client, env, username, role, org_id):
    client.post("/api/users", json={"username": username, "password": "Xquan#2026x",
                                    "role": role, "org_id": org_id}, headers=env["adm"])
    return {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": username, "password": "Xquan#2026x"}
    ).json()["access_token"]}


@pytest.fixture(scope="module")
def p58(client, env, third_party):
    from app.database import SessionLocal
    from app.models import (
        AdminProject, Appointment, AppointmentSlot, ExamReport, ExamRequest, PathologySpecimen,
        ProjectMilestone,
    )
    from app.spd.models import (
        SpdEnrollment, SpdFollowupRecord, SpdIntervention, SpdPackageBinding, SpdProgram,
        SpdQcSample, SpdReferralRule, SpdServicePackage, SpdTarget, SpdTeam, SpdTeamMember,
    )

    A, B = env["A"]["id"], env["B"]["id"]
    doc_a = _login(client, env, "xq_doc_a58", "doctor", A)
    pt = client.post("/api/patients", json={
        "name": "P1-58 探针患者", "id_card": "330102199808081234"}, headers=env["adm"]).json()["id"]
    # 病理申请另用一位患者：乙院给谁开了单，就与谁有了服务关系——若共用 `pt`，
    # "乙院办结甲院患者的干预"会因这层真实关系放行，探针红得不对（实测踩过）
    pt_path = client.post("/api/patients", json={
        "name": "P1-58 病理探针患者", "id_card": "330102199809091234"}, headers=env["adm"]).json()["id"]
    with SessionLocal() as db:
        # 一张申请单只有一份报告：确认接收、处置反馈两条探针各用一张
        reqs = [ExamRequest(patient_id=pt, from_org_id=A, center_type="imaging", item_code="CT01",
                            item_name="头颅CT", created_by=1, status="reported") for _ in range(2)]
        proj = AdminProject(org_id=A, name="P1-58 甲院项目")
        slot = AppointmentSlot(org_id=A, resource_type="doctor", resource_name="甲院专家",
                               slot_date="2026-10-01", capacity=5, booked=1)
        prog = SpdProgram(code="p158", name="P1-58 病种", lead_org_id=A)
        team = SpdTeam(name="甲院团队", org_id=A)
        enr = SpdEnrollment(patient_id=pt, program_code="p158", org_id=A)
        fu = SpdFollowupRecord(patient_id=pt, org_id=A, status="done")
        pkg = SpdServicePackage(code="p158", name="甲院服务包", program_code="p158",
                                items=[{"code": "bp", "times": 5}])
        rule = SpdReferralRule(code="p158", name="甲院病种规则", program_code="p158",
                               conditions=[{"field": "bp_sys", "op": ">=", "value": 180}])
        # 乙院开单、甲院（病理中心）领取的病理申请
        path_req = ExamRequest(patient_id=pt_path, from_org_id=B, center_type="pathology",
                               item_code="BL01", item_name="活检", created_by=1,
                               status="diagnosing", claimed_org_id=A, claimed_by="甲院病理")
        db.add_all([*reqs, proj, slot, prog, team, enr, fu, pkg, rule, path_req]); db.flush()
        reports = [ExamReport(request_id=r.id, conclusion="危急", critical=True,
                              critical_status=st) for r, st in zip(reqs, ("notified", "acknowledged"))]
        ms = [ProjectMilestone(project_id=proj.id, name=f"里程碑{i}", done=done)
              for i, done in enumerate((False, True))]
        appt = Appointment(slot_id=slot.id, patient_id=pt)
        target = SpdTarget(program_id=prog.id, metric="sbp", target_high=140)
        from app.models import User
        a_users = [u.id for u in db.query(User).filter(User.username.in_(("xq_doc_a58", "xq_op_a")))]
        members = [SpdTeamMember(team_id=team.id, user_id=uid) for uid in a_users]
        qc = SpdQcSample(record_id=fu.id)
        interv = SpdIntervention(patient_id=pt, enrollment_id=enr.id, program_code="p158",
                                 content="探针干预")
        bindings = [SpdPackageBinding(enrollment_id=enr.id, package_id=pkg.id,
                                      items=[{"code": "bp", "total": 5, "used": 0}])
                    for _ in range(2)]
        specimens = [PathologySpecimen(request_id=path_req.id, specimen_no=f"P158-{i}")
                     for i in range(3)]
        db.add_all([*reports, *ms, appt, target, *members, qc, interv, *bindings, *specimens])
        db.commit()
        out = {
            "pt": pt, "reports": [r.id for r in reports], "ms": [m.id for m in ms],
            "appt": appt.id, "target": target.id, "members": [m.id for m in members],
            "qc": qc.id, "interv": interv.id, "bindings": [b.id for b in bindings],
            "rule": rule.id, "pkg": pkg.id, "path_req": path_req.id,
            "specimens": [s.id for s in specimens], "doc_a": doc_a,
        }
    # 走接口建的那几样：落库值由服务端从操作人身上取，探针才测得到它
    # 急救：丙院调度、送往甲院
    out["case"] = client.post("/api/emergency/cases", json={
        "location": "P1-58 探针路口", "dest_org_id": A}, headers=third_party["operator"]).json()["id"]
    # 中药：甲院下单、丙院药房领取（推进一步即领取）
    out["tcm"] = client.post("/api/tcm/dispense-orders", json={
        "patient_id": pt, "from_org_id": A, "herbs": "黄芪 30g"}, headers=doc_a).json()["id"]
    assert client.post(f"/api/tcm/dispense-orders/{out['tcm']}/advance",
                       headers=third_party["operator"]).status_code == 200
    # 病理：甲院中心核收第 3 份，供"推进"探针用
    assert client.post(f"/api/pathology/specimens/{out['specimens'][2]}/receive",
                       json={"received_by": "甲院病理"}, headers=doc_a).status_code == 200
    return out


def _p58_cases(env, t, x):
    B_doc, B_op = env["doctor_b"], env["operator_b"]
    return [
        ("危急值：替申请机构确认接收", B_doc, "post",
         f"/api/exams/reports/{x['reports'][0]}/acknowledge", None),
        ("危急值：替申请机构处置反馈", B_doc, "post",
         f"/api/exams/reports/{x['reports'][1]}/resolve", {"note": "探针"}),
        ("项目：把甲院项目的里程碑标完成", B_op, "post",
         f"/api/projects/milestones/{x['ms'][0]}/done", None),
        ("项目：撤销甲院项目里程碑的完成", B_op, "post",
         f"/api/projects/milestones/{x['ms'][1]}/reopen", None),
        ("预约：核销甲院号源上的预约", B_op, "post", f"/api/appointments/{x['appt']}/fulfill", None),
        ("慢专病：办结甲院患者的干预", B_doc, "patch", f"/api/spd/interventions/{x['interv']}",
         {"status": "done"}),
        ("慢专病：改甲院牵头病种的管理目标", B_doc, "patch", f"/api/spd/targets/{x['target']}",
         {"target_high": 200}),
        ("慢专病：关掉甲院团队成员的随访权限", B_doc, "patch",
         f"/api/spd/team-members/{x['members'][0]}", {"can_followup": False}),
        ("慢专病：把甲院团队成员移出", B_doc, "delete",
         f"/api/spd/team-members/{x['members'][1]}", None),
        ("慢专病：给甲院的随访判质控不合格", B_doc, "post",
         f"/api/spd/qc-samples/{x['qc']}/result", {"result": "fail"}),
        ("慢专病：扣减甲院患者的服务包次数", B_op, "post",
         f"/api/spd/package-bindings/{x['bindings'][0]}/usages", {"item_code": "bp"}),
        ("慢专病：解绑甲院患者的服务包", B_doc, "post",
         f"/api/spd/package-bindings/{x['bindings'][1]}/unbind", None),
        ("慢专病：在甲院牵头病种下建转诊规则", B_doc, "post", "/api/spd/referral-rules",
         {"code": "p158-forged", "name": "冒名规则", "program_code": "p158",
          "conditions": [{"field": "bp_sys", "op": ">=", "value": 100}]}),
        ("慢专病：改甲院牵头病种的转诊规则", B_doc, "patch",
         f"/api/spd/referral-rules/{x['rule']}", {"active": False}),
        ("慢专病：在甲院牵头病种下建服务包", B_doc, "post", "/api/spd/service-packages",
         {"code": "p158-forged", "name": "冒名服务包", "program_code": "p158"}),
        ("慢专病：给甲院牵头病种的服务包改价", B_doc, "patch",
         f"/api/spd/service-packages/{x['pkg']}", {"price": 9999}),
        ("急救：推进别家调度的急救事件", B_op, "post", f"/api/emergency/cases/{x['case']}/advance",
         None),
        ("急救：往别家的急救事件回传体征", B_op, "post", f"/api/emergency/cases/{x['case']}/vitals",
         {"heart_rate": 30}),
        ("急救：给别家的急救事件记绿道节点", B_op, "post",
         f"/api/emergency/cases/{x['case']}/milestones",
         {"milestone": "call", "occurred_at": "2026-09-23T08:00:00"}),
        ("中药：推进别家药房已承接的订单", B_op, "post",
         f"/api/tcm/dispense-orders/{x['tcm']}/advance", None),
        ("病理：往别家之间的病理申请登记送检", t["doctor"], "post", "/api/pathology/specimens",
         {"request_id": x["path_req"]}),
        ("病理：核收别家中心的标本", t["doctor"], "post",
         f"/api/pathology/specimens/{x['specimens'][0]}/receive", {"received_by": "丙院"}),
        ("病理：拒收别家中心的标本", t["doctor"], "post",
         f"/api/pathology/specimens/{x['specimens'][1]}/reject", {"reject_reason": "标识不清"}),
        ("病理：推进别家中心已核收的标本", t["doctor"], "post",
         f"/api/pathology/specimens/{x['specimens'][2]}/advance", {}),
    ]


def test_一跳归属与缺处理方的别家实体写入被拦(client, env, p58, third_party):
    """建闸门时这 24 条逐条实打全部放行；删掉对应那一行校验，对应那条必红。"""
    passed_through = []
    for label, who, method, path, body in _p58_cases(env, third_party, p58):
        kw = {"json": body} if method != "delete" else {}
        r = getattr(client, method)(path, headers=who, **kw)
        if r.status_code in (200, 201, 204):
            passed_through.append(f"{label} → {r.status_code}")
        else:
            assert r.status_code == 403, f"{label} 期望 403，实际 {r.status_code}：{r.text[:160]}"
    assert passed_through == [], "以下写入动到了别家的实体：\n  " + "\n  ".join(passed_through)


def test_补了处理方之后各方照常(client, env, p58, third_party):
    """补列不能把单据上**本来就该**动它的那几方关掉。"""
    # 急救：调度方（丙院）推进、接收医院（甲院）回传体征都照常
    r = client.post(f"/api/emergency/cases/{p58['case']}/vitals", json={"heart_rate": 88},
                    headers=p58["doc_a"])
    assert r.status_code == 201, f"接收医院回传体征应照常：{r.text[:160]}"
    r = client.post(f"/api/emergency/cases/{p58['case']}/advance", headers=third_party["operator"])
    assert r.status_code == 200, f"调度方推进应照常：{r.text[:160]}"
    # 中药：承接的药房（丙院）与下单方（甲院）都能推进
    r = client.post(f"/api/tcm/dispense-orders/{p58['tcm']}/advance",
                    headers=third_party["operator"])
    assert r.status_code == 200, f"承接药房推进应照常：{r.text[:160]}"
    r = client.post(f"/api/tcm/dispense-orders/{p58['tcm']}/advance", headers=env["operator_a"])
    assert r.status_code == 200, f"下单方推进应照常：{r.text[:160]}"
    # 病理：核收它的中心（甲院）推进照常
    r = client.post(f"/api/pathology/specimens/{p58['specimens'][2]}/advance", json={},
                    headers=p58["doc_a"])
    assert r.status_code == 200, f"核收中心推进应照常：{r.text[:160]}"


def test_存量急救事件没有调度方照旧不判(client, env):
    """补列前建的事件调度方不可考：只判接收医院会把它的调度方关在门外，所以照旧不判。"""
    from app.database import SessionLocal
    from app.models import EmergencyCase

    with SessionLocal() as db:
        legacy = EmergencyCase(location="存量事件", dest_org_id=env["A"]["id"])
        db.add(legacy); db.commit()
        legacy_id = legacy.id
    r = client.post(f"/api/emergency/cases/{legacy_id}/advance", headers=env["operator_b"])
    assert r.status_code == 200, f"存量事件行为应不变：{r.text[:160]}"


def test_中药订单只能被一家药房承接(client, env, p58, third_party):
    """第一个推进它的非下单机构领取；别家药房再来推进是 403，而下单方推进不领取。"""
    from app.database import SessionLocal
    from app.models import TcmDispenseOrder

    oid = client.post("/api/tcm/dispense-orders", json={
        "patient_id": p58["pt"], "from_org_id": env["A"]["id"], "herbs": "当归 10g"},
        headers=p58["doc_a"]).json()["id"]
    assert client.post(f"/api/tcm/dispense-orders/{oid}/advance",
                       headers=env["operator_a"]).status_code == 200
    with SessionLocal() as db:
        assert db.get(TcmDispenseOrder, oid).pharmacy_org_id is None, "下单方推进不该领取"
    assert client.post(f"/api/tcm/dispense-orders/{oid}/advance",
                       headers=env["operator_b"]).status_code == 200, "首个非下单方应能领取"
    with SessionLocal() as db:
        assert db.get(TcmDispenseOrder, oid).pharmacy_org_id == env["B"]["id"]
    assert client.post(f"/api/tcm/dispense-orders/{oid}/advance",
                       headers=third_party["operator"]).status_code == 403
