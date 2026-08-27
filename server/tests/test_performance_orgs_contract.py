"""特征化网 + 契约：`GET /api/performance/orgs` 机构绩效计分卡。

按 `docs/接口标准与治理.md` 的配方：**先钉住现状、再加 response_model**，
加完网照样绿即证明响应字节没变（CLAUDE.md §11：治理不得改响应字节）。

这个接口的嵌套比多数接口更麻烦：

- `weights` 的**键是动态的**（来自 `performance_indicators` 表，表空时退回默认），
  所以只能是 `dict[str, float]`，不能逐个字段写死；
- `detail` 里**混着两种形状**——三段是 `{分子, 分母}` 小字典、两段是裸计数；
- `score` 是 `round(sum(...), 1)`。这里特意验过：`_normalized_weights` 在表空时
  退回非空的 `DEFAULT_INDICATORS`，`sum()` 永远在浮点上做，所以 `score` **恒为
  float**（`0.0` 而不是 `0`）。若它可能是 int，声明成 `float` 就会把 `0` 变成
  `0.0`——那是改响应字节，不是治理。

网里造了真实数据（转诊完成 1/2、远程诊断 1 次、慢病随访 1/2、处方合格 1/2、
家医服务 2 次）：全零的响应什么都钉不住，比率分母为 0 时代码走的是另一条分支。
"""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import (
    ChronicPatient,
    ContractService,
    DrugRule,
    ExamRequest,
    FamilyDoctorContract,
    FollowUp,
    Organization,
    Patient,
    Prescription,
    PrescriptionItem,
    Referral,
    User,
)


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def seeded(client, admin):
    """一家机构，五个维度各造出"部分完成"的数据，让每个比率都不是 0 也不是 1。

    机构与患者走接口建（`ehc_no` 是接口层生成的，直接插库会撞 NOT NULL），
    五个维度的业务数据直接落库——那几张表各有各的建单流程，绕开更省事也更稳。
    """
    org = client.post(
        "/api/organizations",
        json={"name": "绩效特征化院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    other = client.post(
        "/api/organizations",
        json={"name": "绩效对照院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "绩效患者", "id_card": "330281199505054512"},
        headers=admin,
    ).json()

    with SessionLocal() as db:
        org = db.get(Organization, org["id"])
        other = db.get(Organization, other["id"])
        patient = db.get(Patient, patient["id"])
        doctor = db.query(User).filter(User.username == "admin").first()

        # 转诊 2 条、完成 1 条 → referral 比率 0.5
        for status in ("completed", "pending"):
            db.add(Referral(
                patient_id=patient.id, from_org_id=org.id, to_org_id=other.id,
                direction="up", reason="绩效网", status=status, created_by=doctor.id,
            ))
        # 远程诊断已出报告 1 次 → volume_score(1, cap=5) = 0.2
        db.add(ExamRequest(
            patient_id=patient.id, from_org_id=org.id, center_type="imaging",
            item_code="CT", item_name="胸部CT", status="reported", created_by=doctor.id,
        ))
        # 慢病 2 人、随访到 1 人 → chronic 比率 0.5
        managed = []
        for disease in ("hypertension", "diabetes"):
            cp = ChronicPatient(patient_id=patient.id, disease=disease, managed_by_org_id=org.id)
            db.add(cp)
            managed.append(cp)
        db.commit()
        db.add(FollowUp(chronic_id=managed[0].id, sbp=130.0, dbp=80.0))
        # 处方 2 张、合格 1 张（auto_passed 计不计合格由参数控制）→ rx 比率 0.5
        for status in ("auto_passed", "rejected"):
            db.add(Prescription(
                patient_id=patient.id, org_id=org.id, diagnosis_name="高血压",
                status=status, created_by=doctor.id,
            ))
        # 家医签约服务 2 次 → volume_score(2, cap=5) = 0.4
        contract = FamilyDoctorContract(
            patient_id=patient.id, org_id=org.id, doctor_name="张医生", status="active",
        )
        db.add(contract)
        db.commit()
        db.refresh(contract)
        for service_type in ("visit", "followup"):
            db.add(ContractService(contract_id=contract.id, service_type=service_type))
        db.commit()
        return {"org_id": org.id, "other_id": other.id, "patient_id": patient.id}


def _card(payload, org_id):
    return next(c for c in payload["scorecards"] if c["org_id"] == org_id)


# ---------------------------------------------------------------- 特征化：形状


def test_顶层三段_period必须回给前端(client, admin, seeded):
    """分数从"累计"改成"周期内"之后，不标周期的数字没法解读，故 period 必须回传。"""
    payload = client.get("/api/performance/orgs", headers=admin).json()
    assert set(payload) == {"period", "weights", "scorecards"}
    assert payload["period"] == str(date.today().year), "缺省为当年"


def test_weights键动态且值恒为float(client, admin, seeded):
    """键来自指标表（表空时退回默认），所以契约只能写 dict[str, float]。"""
    weights = client.get("/api/performance/orgs", headers=admin).json()["weights"]
    assert weights, "表空时应退回默认指标，不该是空字典"
    assert all(isinstance(v, float) for v in weights.values())
    assert abs(sum(weights.values()) - 100.0) < 0.05, "权重归一化到 100"


def test_计分卡字段集合与类型(client, admin, seeded):
    card = _card(client.get("/api/performance/orgs", headers=admin).json(), seeded["org_id"])
    assert set(card) == {"org_id", "org_name", "level", "score", "detail"}
    assert isinstance(card["org_id"], int)
    assert isinstance(card["org_name"], str)
    assert isinstance(card["level"], str)
    assert isinstance(card["score"], float), "score 必须是 float，int 会让契约改掉响应字节"
    assert not isinstance(card["score"], bool)


def test_detail的五段形状_三段是分子分母两段是裸计数(client, admin, seeded):
    detail = _card(
        client.get("/api/performance/orgs", headers=admin).json(), seeded["org_id"]
    )["detail"]
    assert set(detail) == {
        "referral_completion", "remote_exams", "remote_exams_requested",
        "remote_exams_provided", "chronic_followup", "rx_pass", "contract_services",
    }
    assert detail["referral_completion"] == {"completed": 1, "total": 2}
    assert detail["chronic_followup"] == {"followed": 1, "total": 2}
    # rule_covered：本期"至少一味药对得上生效规则"的处方张数（口径裁定 4）。
    # seeded 没建 drug_rules，故为 0——真能数出非零的场景见
    # test_规则覆盖数只数对得上生效规则的处方。
    assert detail["rx_pass"] == {"passed": 1, "total": 2, "rule_covered": 0}
    # remote_exams 是**计分值**。当前口径（2026-08-27 回退待批）= 仅申请方一侧；
    # 中心侧 provided 单独展示、不入计分。卫健批复后此不变量翻转为"两侧之和"。
    assert detail["remote_exams"] == 1
    assert detail["remote_exams_requested"] == 1
    assert detail["remote_exams_provided"] == 0
    assert detail["remote_exams"] == detail["remote_exams_requested"], (
        "计分值必须等于申请方侧，否则明细解释不了分数（回退口径，见 performance.py）"
    )
    assert detail["contract_services"] == 2


def test_无数据机构各段也齐全且为零(client, admin, seeded):
    """比率分母为 0 时走的是另一条分支，字段不能因此消失。"""
    card = _card(client.get("/api/performance/orgs", headers=admin).json(), seeded["other_id"])
    assert card["score"] == 0.0
    assert card["detail"]["referral_completion"] == {"completed": 0, "total": 0}
    assert card["detail"]["remote_exams"] == 0


# ---------------------------------------------------------------- 特征化：口径


def test_score按权重折算并保留一位小数(client, admin, seeded):
    """钉住折算值本身——重构分数公式时这条会立刻报警。

    默认权重 referral 20 / remote_exam 20 / chronic 25 / rx 20 / contract 15，
    比率 0.5 / 0.2 / 0.5 / 0.5 / 0.4 → 10 + 4 + 12.5 + 10 + 6 = 42.5
    """
    card = _card(client.get("/api/performance/orgs", headers=admin).json(), seeded["org_id"])
    assert card["score"] == 42.5


def test_include_auto_passed收紧口径会降低rx段(client, admin, seeded):
    """传 False 时只认药师人工审核通过——本网里 auto_passed 那张不再算合格。"""
    payload = client.get(
        "/api/performance/orgs?include_auto_passed=false", headers=admin
    ).json()
    card = _card(payload, seeded["org_id"])
    assert card["detail"]["rx_pass"] == {"passed": 0, "total": 2, "rule_covered": 0}
    assert card["score"] == 32.5, "rx 段 20 分归零，总分从 42.5 降到 32.5"


def test_volume_cap改变量类维度折算(client, admin, seeded):
    """cap=1 时远程诊断 1 次即满分，家医 2 次也封顶。"""
    payload = client.get("/api/performance/orgs?volume_cap=1", headers=admin).json()
    card = _card(payload, seeded["org_id"])
    assert card["score"] == 67.5, "remote_exam 与 contract 两段满分：10+20+12.5+10+15"


def test_按分数倒序(client, admin, seeded):
    scores = [c["score"] for c in client.get("/api/performance/orgs", headers=admin).json()["scorecards"]]
    assert scores == sorted(scores, reverse=True)


def test_volume_cap非法拒绝(client, admin, seeded):
    assert client.get("/api/performance/orgs?volume_cap=0", headers=admin).status_code == 422


# ---------------------------------------------------------------- 周期口径


def test_上一年度的业务量不计入本年得分(client, admin, seeded):
    """口径变更的核心断言：本接口此前算的是**开天辟地累计**。

    往去年塞一批转诊，本年度的分数**必须一点都不动**——若还是累计口径，
    分母会变大、结案率会变，分数就会掉。
    """
    from app.models import Referral

    before = _card(
        client.get("/api/performance/orgs", headers=admin).json(), seeded["org_id"]
    )["score"]

    last_year = date.today().year - 1
    with SessionLocal() as db:
        doctor = db.query(User).filter(User.username == "admin").first()
        for _ in range(20):
            db.add(Referral(
                patient_id=seeded["patient_id"], from_org_id=seeded["org_id"],
                to_org_id=seeded["other_id"], direction="up", reason="去年的",
                status="pending", created_by=doctor.id,
                created_at=datetime(last_year, 6, 1, 12, 0),
            ))
        db.commit()

    after = _card(
        client.get("/api/performance/orgs", headers=admin).json(), seeded["org_id"]
    )["score"]
    assert after == before, (
        f"去年的 20 条转诊影响了本年得分（{before} → {after}）——说明时间窗没生效"
    )


def test_显式指定去年能看到去年的数(client, admin, seeded):
    """周期口径的另一半：指定哪一期就该算哪一期，不能只会算当年。

    **自己造去年的数据**，不依赖上一条用例插进去的那批——模块级 DB 下
    "靠前一条留下的状态"会让本条单跑即红（`-k` / `-x` / 分片 / 随机序都会踩）。
    这个坑本轮之前在 `test_org_tree_health` 上踩过一次，不该再踩第二次。
    """
    from app.models import Referral

    last_year = date.today().year - 1
    marker_org = seeded["org_id"]
    with SessionLocal() as db:
        doctor = db.query(User).filter(User.username == "admin").first()
        # 先清掉可能由别的用例留下的去年数据，再造自己的
        db.query(Referral).filter(
            Referral.created_at < datetime(date.today().year, 1, 1)
        ).delete(synchronize_session=False)
        for status in ("completed", "completed", "pending"):
            db.add(Referral(
                patient_id=seeded["patient_id"], from_org_id=marker_org,
                to_org_id=seeded["other_id"], direction="up", reason="去年的",
                status=status, created_by=doctor.id,
                created_at=datetime(last_year, 6, 1, 12, 0),
            ))
        db.commit()

    payload = client.get(f"/api/performance/orgs?period={last_year}", headers=admin).json()
    assert payload["period"] == str(last_year)
    card = _card(payload, marker_org)
    assert card["detail"]["referral_completion"] == {"completed": 2, "total": 3}
    assert card["detail"]["rx_pass"] == {"passed": 0, "total": 0, "rule_covered": 0}, "去年没有处方"


def test_慢病口径不对称_分母是存量分子按期(client, admin, seeded):
    """分母若也按期，指标就变成"本期新入组的有多少随访过"，那是另一个东西。

    构造能**区分**两种实现的场景：两名慢病患者**去年就入组了、今年仍在管**，
    其中一人在**本年**随访过。

    - 正确（分母存量、分子按期）：total=2、followed=1
    - 错误（分母也按期）：total=0——去年入组的人凭空从分母里消失，
      而他们明明还在管。这正是"在管覆盖率"与"新入组随访率"的区别。

    （第一版这条用例没造这个场景——患者建于当下，把分母按期过滤掉也照样是 2，
    变异测试把它揪了出来。）
    """
    from app.models import ChronicPatient, FollowUp

    last_year = datetime(date.today().year - 1, 3, 1, 12, 0)
    with SessionLocal() as db:
        # 两名患者去年入组，今年仍在管
        for cp in db.query(ChronicPatient).all():
            cp.created_at = last_year
        # 其中一人的随访保持在本年（fixture 里就是当下建的）
        assert db.query(FollowUp).count() == 1
        db.commit()

    card = _card(client.get("/api/performance/orgs", headers=admin).json(), seeded["org_id"])
    assert card["detail"]["chronic_followup"] == {"followed": 1, "total": 2}, (
        "分母必须是在管存量 2（去年入组的人今年仍要考核），分子是本期随访到的 1"
    )


def test_月度粒度(client, admin, seeded):
    month = date.today().strftime("%Y-%m")
    payload = client.get(f"/api/performance/orgs?period={month}", headers=admin).json()
    assert payload["period"] == month
    # 本月建的数据仍在窗口内
    assert _card(payload, seeded["org_id"])["detail"]["rx_pass"]["total"] == 2


def test_非法period拒绝(client, admin, seeded):
    for bad in ("2026-13-01", "abc", "20261"):
        r = client.get(f"/api/performance/orgs?period={bad}", headers=admin)
        assert r.status_code == 422, f"{bad} 应被拒绝，实际 {r.status_code}"


# ---------------------------------------------------------------- 内部调用方


def test_历史周期的分数可复现_新入组不得改旧分(client, admin, seeded):
    """分母只设上界不设下界：去年的分数不能随今年新入组的人漂移。

    没有上界的话，查 2025 年度会把 2026 才入组的人算进分母——
    历史分数每天都在变，考核结论无从复核。
    """
    from app.models import ChronicPatient

    last_year = str(date.today().year - 1)
    before = _card(
        client.get(f"/api/performance/orgs?period={last_year}", headers=admin).json(),
        seeded["org_id"],
    )["detail"]["chronic_followup"]

    with SessionLocal() as db:                       # 今年新入组一个
        db.add(ChronicPatient(
            patient_id=seeded["patient_id"], disease="copd",
            managed_by_org_id=seeded["org_id"],
        ))
        db.commit()

    after = _card(
        client.get(f"/api/performance/orgs?period={last_year}", headers=admin).json(),
        seeded["org_id"],
    )["detail"]["chronic_followup"]
    assert after == before, f"今年新入组改变了去年的分母：{before} → {after}"


def test_基金分配用池子年度的分数而不是当年(client, admin, seeded):
    """`fund.distribute()` 内部调 `org_scorecards()`——绩效改周期口径后，
    不显式传 `period` 就会拿"次年至今"的空白分数去分上一年度的钱。

    基金池结算通常就发生在次年年初，这不是边角场景。
    """
    import inspect

    from app.routers import fund

    source = inspect.getsource(fund.distribute)
    assert "org_scorecards(" in source
    assert "period=str(pool.year)" in source, (
        "分配基金必须按池子所属年度取分数，否则次年初结算会拿到近乎空白的当年分数"
    )


def test_运营月报的绩效分跟着报表周期(client, admin, seeded):
    """CSV 其余各列都按 period 过滤，分数列不跟就会一行里两个口径。"""
    import inspect

    from app.routers import reports

    source = inspect.getsource(reports.export_operations_csv)
    assert "org_scorecards(period=period" in source, "绩效分列必须跟着报表周期"
    assert "score_period" in source, "表头要注明分数所属周期"


def test_慢病随访按人去重_同一人随访多次只算一次(client, admin):
    """覆盖率的分子是"被随访到的人"，不是"随访的次数"。

    漏掉去重，一个被随访 3 次的患者会把分子顶到 3——在管 2 人却"覆盖 3 人"，
    比率 >100%、慢病维度直接超分。这条独立建数据，不蹭 seeded（那里每人恰好
    只有一次随访，正好测不出去重）。
    """
    org = client.post(
        "/api/organizations",
        json={"name": "随访去重院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients", json={"name": "多次随访患者", "id_card": "330281199603034518"},
        headers=admin,
    ).json()
    with SessionLocal() as db:
        cp = ChronicPatient(
            patient_id=patient["id"], disease="hypertension", managed_by_org_id=org["id"]
        )
        db.add(cp)
        other = ChronicPatient(
            patient_id=patient["id"], disease="diabetes", managed_by_org_id=org["id"]
        )
        db.add(other)
        db.commit()
        for sbp in (130.0, 135.0, 140.0):          # 同一人随访 3 次
            db.add(FollowUp(chronic_id=cp.id, sbp=sbp, dbp=80.0))
        db.commit()

    card = _card(
        client.get("/api/performance/orgs", headers=admin).json(), org["id"]
    )
    seg = card["detail"]["chronic_followup"]
    assert seg == {"followed": 1, "total": 2}, seg
    assert seg["followed"] <= seg["total"], "覆盖人数超过在管人数，说明分子按次数算了"


def test_查询条数不随机构数增长(client, admin):
    """N+1 防复发：机构数翻几倍，SQL 条数必须一条不变。

    原实现每家机构 8 条 count，200 家就是 1600 条往返；改成 8 条 `GROUP BY org_id`
    后条数与机构数解耦。用"两次不同机构数的实测差值"来判定，而不是写死一个
    条数上限——上限会被无关改动（多一次鉴权查询）误伤，差值不会。
    """
    from sqlalchemy import event

    from app.database import engine

    def count_sql(n_new_orgs: int) -> int:
        for i in range(n_new_orgs):
            client.post(
                "/api/organizations",
                json={"name": f"N加一测试院{n_new_orgs}_{i}", "org_type": "township",
                      "level": "township"},
                headers=admin,
            )
        seen = []

        def hook(conn, cursor, statement, params, context, executemany):
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", hook)
        try:
            resp = client.get("/api/performance/orgs", headers=admin)
            assert resp.status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", hook)
        return len(seen)

    first = count_sql(2)
    second = count_sql(12)
    assert second == first, (
        f"多 10 家机构多跑了 {second - first} 条 SQL——N+1 回来了"
    )
    # 防呆：探针本身得真的在数（挂不上钩子时两边都是 0，上面的相等断言恒真）
    assert first > 0, "SQL 探针一条都没抓到，本用例是空转"



def test_规则覆盖数只数对得上生效规则的处方(client, admin):
    """口径裁定 4：`auto_passed` 的真实含义是"没有规则被触发"，不是"药师看过"。

    `drug_rules` 是全县共用的一张表，按药品编码维护。规则库越稀疏，越多处方是
    "无规则可审"地自动通过，`passed/total` 就越接近 100% 而越不反映用药合理性。
    所以把"可审张数"一并给出。这条证明它真的在数，且数的是对的：

    - 建了生效规则的药 → 计入；
    - **停用**的规则 → 不计入（`_active_rule` 把停用等同未维护，这里必须一致）；
    - 没建规则的药 → 不计入。
    """
    org = client.post(
        "/api/organizations",
        json={"name": "规则覆盖院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients", json={"name": "规则覆盖患者", "id_card": "330281199704044519"},
        headers=admin,
    ).json()
    with SessionLocal() as db:
        db.add(DrugRule(drug_code="RC_ACTIVE", max_daily_dose=100.0, active=True))
        db.add(DrugRule(drug_code="RC_STOPPED", max_daily_dose=100.0, active=False))
        doctor = db.query(User).filter(User.username == "admin").first()
        for code in ("RC_ACTIVE", "RC_STOPPED", "RC_NO_RULE"):
            rx = Prescription(patient_id=patient["id"], org_id=org["id"],
                              diagnosis_name="高血压", status="auto_passed",
                              created_by=doctor.id)
            db.add(rx)
            db.flush()
            db.add(PrescriptionItem(prescription_id=rx.id, drug_code=code,
                                    drug_name=code, daily_dose=1.0, days=1))
        db.commit()

    seg = _card(client.get("/api/performance/orgs", headers=admin).json(),
                org["id"])["detail"]["rx_pass"]
    assert seg["total"] == 3, seg
    assert seg["passed"] == 3, "三张都是 auto_passed，默认口径下都算合格"
    assert seg["rule_covered"] == 1, (
        f"只有 RC_ACTIVE 那张有生效规则可审，实际 {seg['rule_covered']}——"
        "停用规则必须与未维护同路处理（与 prescriptions._active_rule 一致）"
    )
    # 这正是本裁定要让人看见的事：合格率 100%，但三张里只有一张真被规则审过。
    assert seg["passed"] == seg["total"] and seg["rule_covered"] < seg["total"]


def test_一张处方有多味药命中规则也只算一张(client, admin):
    """按处方去重——不去重的话覆盖数会超过总张数，比率大于 1。"""
    org = client.post(
        "/api/organizations",
        json={"name": "多味药院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients", json={"name": "多味药患者", "id_card": "330281199805054516"},
        headers=admin,
    ).json()
    with SessionLocal() as db:
        for code in ("MD_A", "MD_B"):
            db.add(DrugRule(drug_code=code, max_daily_dose=100.0, active=True))
        doctor = db.query(User).filter(User.username == "admin").first()
        rx = Prescription(patient_id=patient["id"], org_id=org["id"], diagnosis_name="高血压",
                          status="auto_passed", created_by=doctor.id)
        db.add(rx)
        db.flush()
        for code in ("MD_A", "MD_B"):
            db.add(PrescriptionItem(prescription_id=rx.id, drug_code=code, drug_name=code,
                                    daily_dose=1.0, days=1))
        db.commit()

    seg = _card(client.get("/api/performance/orgs", headers=admin).json(),
                org["id"])["detail"]["rx_pass"]
    assert seg == {"passed": 1, "total": 1, "rule_covered": 1}, seg


# ------------------------------------------------ 口径裁定 1、2 的特征化（分数不变）
def test_结案率按转出机构计_接收方分母里没有这张单(client, admin):
    """口径裁定 1：转诊结案率的分子分母都按 `from_org_id`（转出方）。

    含义是接收方把单子结案了、功劳记在转出方头上，而接收方自己的分母里
    根本没有这张单。保留这个口径的前提是**分子不能被任意机构改**——
    见 `test_referral_status_authority.py`：改这条口径之前，任何机构的医师
    都能把别人的单子结案，那时讨论分母属于谁毫无意义。
    """
    def org(name):
        return client.post("/api/organizations",
                           json={"name": name, "org_type": "township", "level": "township"},
                           headers=admin).json()

    sender, receiver = org("结案率转出院"), org("结案率接收院")
    patient = client.post("/api/patients",
                          json={"name": "结案率患者", "id_card": "330281199906064511"},
                          headers=admin).json()
    ref = client.post("/api/referrals",
                      json={"patient_id": patient["id"], "from_org_id": sender["id"],
                            "to_org_id": receiver["id"], "direction": "up", "reason": "上转"},
                      headers=admin).json()
    for status in ("accepted", "completed"):
        client.patch(f"/api/referrals/{ref['id']}/status", json={"status": status}, headers=admin)

    payload = client.get("/api/performance/orgs", headers=admin).json()
    assert _card(payload, sender["id"])["detail"]["referral_completion"] == {
        "completed": 1, "total": 1}, "转出方应拿到这张单的分子与分母"
    assert _card(payload, receiver["id"])["detail"]["referral_completion"] == {
        "completed": 0, "total": 0}, "接收方的分母里不该出现这张单（当前口径）"


def test_互认计入申请方_中心出报告量可见但不入计分(client, admin):
    """口径裁定 2 + 中心侧计分回退（2026-08-27，待卫健批复）。

    - **申请方**（`from_org_id`）：`reported` + `recognized` 计入 `remote_exams`
      （计分值）。互认照计——这一侧衡量的是"通过平台解决了多少次检查需求"，
      不重复做检查正是想要的结果；
    - **共享诊断中心**（`claimed_org_id`）：出报告量在 `remote_exams_provided`
      里**可见**，但**不进计分值**。2026-08-22 曾改为两侧都计，因该口径改变基金
      分配却查不到卫健批复记录，按治理默认回退；批复后恢复见 performance.py
      端点 docstring（一行 + 本文件两条哨兵用例翻转）。

    `provided` 只数 `reported`：互认不产生中心工作量，而且互认单结构上就没有
    `claimed_org_id`（`recognized` 是建单时定下的状态，领取只发生在 `pending` 上）
    ——本用例把这条结构性事实也断言住。
    """
    def org(name):
        return client.post("/api/organizations",
                           json={"name": name, "org_type": "township", "level": "township"},
                           headers=admin).json()

    grassroots, center = org("互认基层院"), org("互认诊断中心")
    patient = client.post("/api/patients",
                          json={"name": "互认患者", "id_card": "330281200001014513"},
                          headers=admin).json()
    with SessionLocal() as db:
        doctor = db.query(User).filter(User.username == "admin").first()
        # 中心领取并出了报告的两张
        for _ in range(2):
            db.add(ExamRequest(patient_id=patient["id"], from_org_id=grassroots["id"],
                               claimed_org_id=center["id"], center_type="imaging",
                               item_code="CT", item_name="胸部CT", status="reported",
                               created_by=doctor.id))
        # 互认一张：申请方计入，中心不计（也没有 claimed_org_id）
        db.add(ExamRequest(patient_id=patient["id"], from_org_id=grassroots["id"],
                           center_type="imaging", item_code="CT", item_name="胸部CT",
                           status="recognized", created_by=doctor.id))
        # 还没领取的一张：两侧都不计
        db.add(ExamRequest(patient_id=patient["id"], from_org_id=grassroots["id"],
                           center_type="imaging", item_code="CT", item_name="胸部CT",
                           status="pending", created_by=doctor.id))
        db.commit()

    payload = client.get("/api/performance/orgs", headers=admin).json()
    g = _card(payload, grassroots["id"])["detail"]
    c = _card(payload, center["id"])["detail"]

    assert g["remote_exams_requested"] == 3, "2 张已报告 + 1 张互认；pending 不算"
    assert g["remote_exams_provided"] == 0, "基层不是中心"
    assert g["remote_exams"] == 3

    assert c["remote_exams_provided"] == 2, (
        f"中心出的 2 份报告必须**可见**，实际 {c['remote_exams_provided']}——"
        "这个字段是'中心工作量被看见但没算分'的哨兵，删掉它这件事就彻底隐形了"
    )
    assert c["remote_exams_requested"] == 0, "中心没有以申请方身份建过单"
    assert c["remote_exams"] == 0, (
        "当前口径（回退待批）计分值只取申请方一侧；中心的 2 份报告不该进计分值。"
        "卫健批复后本断言翻转为 == 2"
    )


def test_互认单永远不计给中心(client, admin):
    """即便硬把 claimed_org_id 塞进一张互认单，中心侧也不该数它。

    互认不产生诊断工作量——中心侧只数 `reported`，这条守的是那个 filter。
    """
    center = client.post("/api/organizations",
                         json={"name": "互认不计中心", "org_type": "lead_hospital",
                               "level": "county"}, headers=admin).json()
    patient = client.post("/api/patients",
                          json={"name": "互认不计患者", "id_card": "330281200102024517"},
                          headers=admin).json()
    with SessionLocal() as db:
        doctor = db.query(User).filter(User.username == "admin").first()
        db.add(ExamRequest(patient_id=patient["id"], from_org_id=center["id"],
                           claimed_org_id=center["id"], center_type="imaging",
                           item_code="CT", item_name="胸部CT", status="recognized",
                           created_by=doctor.id))
        db.commit()
    detail = _card(client.get("/api/performance/orgs", headers=admin).json(),
                   center["id"])["detail"]
    assert detail["remote_exams_provided"] == 0, "互认单被算进中心工作量了"
    assert detail["remote_exams_requested"] == 1, "作为申请方仍应计入"


def test_中心出报告暂不改变得分_待卫健批复(client, admin):
    """哨兵（2026-08-27 回退待批）：中心出再多报告，`score` 也不动。

    这不是说"中心不该得分"——多半该得。但这个口径决定各机构分多少钱，
    实现方不能替卫健拍板；2026-08-22 那次实施查不到批复记录，故回退到
    "只按申请方计分"的既批口径。**批复到位后本用例翻转为 `after > before`**
    （恢复点见 performance.py 端点 docstring），届时中心侧计分即生效。
    在那之前，中心的工作量由 `remote_exams_provided` 字段保持可见。
    """
    center = client.post("/api/organizations",
                         json={"name": "中心得分院", "org_type": "lead_hospital",
                               "level": "county"}, headers=admin).json()
    before = _card(client.get("/api/performance/orgs", headers=admin).json(),
                   center["id"])["score"]
    patient = client.post("/api/patients",
                          json={"name": "中心得分患者", "id_card": "330281200203034512"},
                          headers=admin).json()
    with SessionLocal() as db:
        doctor = db.query(User).filter(User.username == "admin").first()
        for _ in range(5):                      # 若计分，这个量足以顶到 volume_cap
            db.add(ExamRequest(patient_id=patient["id"], from_org_id=1,
                               claimed_org_id=center["id"], center_type="imaging",
                               item_code="CT", item_name="胸部CT", status="reported",
                               created_by=doctor.id))
        db.commit()
    card = _card(client.get("/api/performance/orgs", headers=admin).json(), center["id"])
    assert card["score"] == before, (
        f"回退期间中心出报告不该改变得分（{before} → {card['score']}）——"
        "若这是有意恢复中心侧计分，请先确认卫健批复，再按 docstring 的恢复点整体翻转"
    )
    assert card["detail"]["remote_exams_provided"] == 5, "工作量必须保持可见"
