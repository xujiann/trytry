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
