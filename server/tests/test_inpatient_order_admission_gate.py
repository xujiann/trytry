"""住院状态迁移（出院、转床）压进带条件 UPDATE 之后的回归（P1-30）。

这两条不变式不长在 `admissions` 的**键**上，而长在**状态行**上：

- 一条住院记录只能从 `admitted` 迁到 `discharged` **一次**；
- 在院期间换床，只能从"我读到的那张床"上换走。

洞的形状与 P1-29 那批"先查有没有、没有就建"同源，只是判定对象从"有没有行"
换成了"行处在哪一态"：出院是 `读 status → 判 admitted → 赋值 → commit`，
转床是 `读 bed_id → 占新床 → 无条件释旧床 → 赋值`。PG 的 READ COMMITTED 下
两路都读到 admitted，于是——

- **两路出院**：床释放两次、出院随访与站内通知各派生一份、
  ADMISSION_DISCHARGED 发布两次、`discharged_at` 以最后提交的为准。
  平台端点与 HL7 A03 镜像是同一行的两个出院入口，互相之间也会撞；
- **两路转床** A→B 与 A→C：两张目标床都被占上，`bed_id` 只留下最后写的那个，
  另一张**永远占着、没有任何住院记录挂在它上面**——只能人工去改库。

修法是把判定压进 UPDATE 的 WHERE（`_mark_discharged` / 转床的比较交换），
rowcount 为 0 即回滚 + 409，且文案与顺序重复完全一致。

**SQLite 上线程探针证不了这些**（库级写锁把两路排开，窗口根本不打开），所以这里
用侧信道确定性地复现"赢家在预检与闸门之间提交"这一刻——那正是 PG 上抢输者
实际到达的位置。八路真并发在 `tests/test_inpatient_order_unique_races.py`。
"""
import ast
import inspect
import pathlib
import textwrap

import pytest
from sqlalchemy import update

from conftest import login

import app.routers.inpatient as inpatient_router
from app.database import SessionLocal
from app.models import Admission, Bed, FollowupTask, InpatientOrder, utcnow

ROUTERS_DIR = pathlib.Path(inspect.getfile(inpatient_router)).parent


@pytest.fixture(scope="module")
def ward(client, admin):
    """一个机构 + 两个病区 + 十张床：每条用例占各自的床，互不串味。"""
    org = client.post(
        "/api/organizations",
        json={"name": "状态迁移县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    first = client.post(
        "/api/inpatient/wards",
        json={"org_id": org["id"], "name": "迁移一区"},
        headers=admin,
    ).json()
    second = client.post(
        "/api/inpatient/wards",
        json={"org_id": org["id"], "name": "迁移二区"},
        headers=admin,
    ).json()
    beds = [
        client.post(
            "/api/inpatient/beds",
            json={"ward_id": first["id"], "bed_no": f"AG-{i}"},
            headers=admin,
        ).json()
        for i in range(10)
    ]
    other_bed = client.post(
        "/api/inpatient/beds",
        json={"ward_id": second["id"], "bed_no": "AG-他区"},
        headers=admin,
    ).json()
    return {"org": org, "ward": first, "other_ward": second, "beds": beds, "other_bed": other_bed}


@pytest.fixture(scope="module")
def doctor(client, admin, ward):
    """出院/转床限 doctor 角色（既有业务规则），单开一个本机构医师账号。"""
    client.post(
        "/api/users",
        json={"username": "ag_doc", "password": "pass123456", "role": "doctor",
              "org_id": ward["org"]["id"], "full_name": "艾医生"},
        headers=admin,
    )
    return login(client, "ag_doc", "pass123456")


def _admit(client, admin, ward, name, id_card, bed_index):
    patient = client.post(
        "/api/patients",
        json={"name": name, "id_card": id_card, "gender": "女", "birth_date": "1985-06-06"},
        headers=admin,
    ).json()
    created = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["ward"]["id"],
              "bed_id": ward["beds"][bed_index]["id"], "doctor_name": "艾医生"},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    return patient, created.json()


def _case_summary(client, doctor, admission_id):
    resp = client.post(
        f"/api/inpatient/admissions/{admission_id}/case-summary",
        json={"discharge_diagnosis": "急性支气管炎", "outcome": "治愈"},
        headers=doctor,
    )
    assert resp.status_code == 201, resp.text


def _fire_once_after(monkeypatch, attr, action):
    """在 inpatient 路由里某个函数返回之后，用**另一条连接**提交一笔并发写。

    这是"赢家在预检与闸门之间提交"的确定性复现，也是抢输的一路在 PG 上真正
    到达的位置：处理器手上那个 ORM 对象仍是锁外读到的旧值，闸门却要按库里的
    新值重算 WHERE。可以这么做是因为 pysqlite 在处理器发出第一条 DML 之前
    不发 BEGIN——此刻另开会话提交不会 `database is locked`；闸门一旦执行，
    这个窗口就关上了，所以钩子必须挂在闸门之前的那一句上。
    """
    original = getattr(inpatient_router, attr)
    fired: list[bool] = []

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        if not fired:
            fired.append(True)
            db = SessionLocal()
            try:
                action(db)
                db.commit()
            finally:
                db.close()
        return result

    monkeypatch.setattr(inpatient_router, attr, wrapper)
    return fired


def _bed_status(client, admin, bed_id, ward_id):
    beds = client.get(f"/api/inpatient/beds?ward_id={ward_id}", headers=admin).json()
    return next(b["status"] for b in beds if b["id"] == bed_id)


# ================================================================ 出院闸门


def test_二次出院仍是同一句409且出院随访恰一条(client, admin, ward, doctor):
    _, admission = _admit(client, admin, ward, "迁移甲", "330281198506060026", 0)
    _case_summary(client, doctor, admission["id"])

    first = client.post(f"/api/inpatient/admissions/{admission['id']}/discharge", headers=doctor)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "discharged" and first.json()["discharged_at"]

    again = client.post(f"/api/inpatient/admissions/{admission['id']}/discharge", headers=doctor)
    assert again.status_code == 409
    assert again.json()["detail"] == "该患者已出院"

    db = SessionLocal()
    try:
        tasks = (
            db.query(FollowupTask)
            .filter(FollowupTask.category == "discharge",
                    FollowupTask.source_id == admission["id"])
            .all()
        )
        assert len(tasks) == 1, "一次出院只该派生一条出院随访"
    finally:
        db.close()
    assert _bed_status(client, admin, ward["beds"][0]["id"], ward["ward"]["id"]) == "free"


def test_出院闸门抢输者不派生随访不释床不停医嘱(client, admin, ward, doctor, monkeypatch):
    """赢家在预检与闸门之间提交：抢输的一路必须一行都没动过。

    断言的三样都在闸门**之后**：出院随访、床位释放、医嘱批量停止。任何一样被
    抢输者做掉，都说明闸门没排在第一条写语句的位置上。
    """
    _, admission = _admit(client, admin, ward, "迁移乙", "330281198506060042", 1)
    aid = admission["id"]
    _case_summary(client, doctor, aid)
    order = client.post(
        "/api/inpatient/orders",
        json={"admission_id": aid, "order_type": "long", "content": "低流量吸氧 qd"},
        headers=doctor,
    )
    assert order.status_code == 201, order.text

    def winner(db):
        db.execute(
            update(Admission)
            .where(Admission.id == aid, Admission.status == "admitted")
            .values(status="discharged", discharged_at=utcnow())
        )

    # utcnow() 是出院处理器在闸门前的最后一句（`now = utcnow()`），钩在这里最贴近窗口
    fired = _fire_once_after(monkeypatch, "utcnow", winner)

    resp = client.post(f"/api/inpatient/admissions/{aid}/discharge", headers=doctor)
    assert fired, "侧信道没触发，这条用例什么也没证明"
    assert resp.status_code == 409
    assert resp.json()["detail"] == "该患者已出院", "抢输者与顺序重复必须拿到同一句话"

    db = SessionLocal()
    try:
        assert db.query(FollowupTask).filter(
            FollowupTask.category == "discharge", FollowupTask.source_id == aid
        ).count() == 0, "抢输者不该派生出院随访"
        assert db.get(Bed, ward["beds"][1]["id"]).status == "occupied", "抢输者不该释放床位"
        assert db.query(InpatientOrder).filter(
            InpatientOrder.admission_id == aid, InpatientOrder.status == "active"
        ).count() == 1, "抢输者不该批量停医嘱"
    finally:
        db.close()


def test_A03镜像重复出院拿同一句409(client, admin, ward):
    """HL7 A03 是同一行的另一个出院入口，抢输与顺序重复的文案必须一致。"""
    id_card = "330281198506060069"
    patient = client.post(
        "/api/patients",
        json={"name": "迁移镜像", "id_card": id_card, "gender": "女", "birth_date": "1985-06-06"},
        headers=admin,
    ).json()
    created = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["ward"]["id"],
              "bed_id": ward["beds"][8]["id"], "doctor_name": "艾医生"},
        headers=admin,
    )
    assert created.status_code == 201, created.text

    message = (
        "MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260821090000||ADT^A03|MSGAG03|P|2.4\r"
        f"PID|1||{id_card}^^^CN^ID||迁移镜像||19850606|F"
    )
    first = client.post("/api/integration/hl7v2/adt", json={"message": message}, headers=admin)
    assert first.status_code == 201, first.text
    assert first.json()["admission_id"] == created.json()["id"]

    again = client.post("/api/integration/hl7v2/adt", json={"message": message}, headers=admin)
    assert again.status_code == 409
    assert again.json()["detail"] == "该患者无在院记录，A03 出院拒收"
    assert _bed_status(client, admin, ward["beds"][8]["id"], ward["ward"]["id"]) == "free"


# ================================================================ 转床比较交换


def test_转床抢输给另一台转床拿刷新提示且不漏占床(client, admin, ward, doctor, monkeypatch):
    """两台工作站同时把同一位患者转走：抢输的一路必须什么都不占。

    修复前两张目标床都被占上、`bed_id` 只留最后写的那个，另一张永远占着且
    没有住院记录挂在上面。这里断言的是**状态不变量**：全病区恰有一张占用床，
    且它就是住院记录当前那张。
    """
    _, admission = _admit(client, admin, ward, "迁移丙", "330281198506060085", 2)
    aid = admission["id"]
    from_bed, rival_bed, my_bed = (ward["beds"][i]["id"] for i in (2, 3, 4))

    def rival(db):
        db.execute(
            update(Admission)
            .where(Admission.id == aid, Admission.status == "admitted",
                   Admission.bed_id == from_bed)
            .values(bed_id=rival_bed)
        )
        db.execute(update(Bed).where(Bed.id == rival_bed).values(status="occupied"))
        db.execute(update(Bed).where(Bed.id == from_bed).values(status="free"))

    # assert_obj_org_writable 是转床处理器读完 admission 之后、状态预检之前的一句
    fired = _fire_once_after(monkeypatch, "assert_obj_org_writable", rival)

    resp = client.post(
        f"/api/inpatient/admissions/{aid}/transfer",
        json={"ward_id": ward["ward"]["id"], "bed_id": my_bed},
        headers=doctor,
    )
    assert fired, "侧信道没触发，这条用例什么也没证明"
    assert resp.status_code == 409
    assert resp.json()["detail"] == "床位信息刚被其他操作变更，请刷新后重试"

    db = SessionLocal()
    try:
        assert db.get(Admission, aid).bed_id == rival_bed
        assert db.get(Bed, my_bed).status == "free", "抢输者占上的床没退回来，就是漏床"
        assert db.get(Bed, rival_bed).status == "occupied"
        assert db.get(Bed, from_bed).status == "free"
    finally:
        db.close()


def test_转床抢输给出院仍是仅在院患者可转科转床(client, admin, ward, doctor, monkeypatch):
    """输给的是一次出院时，文案必须回落到既有那句——否则调用方能从措辞上
    分辨"并发撞车"与"本来就已出院"，而顺序请求下这两者本就该没有区别。"""
    _, admission = _admit(client, admin, ward, "迁移丁", "330281198506060107", 5)
    aid = admission["id"]
    from_bed, my_bed = ward["beds"][5]["id"], ward["beds"][6]["id"]

    def rival(db):
        db.execute(
            update(Admission)
            .where(Admission.id == aid, Admission.status == "admitted")
            .values(status="discharged", discharged_at=utcnow())
        )
        db.execute(update(Bed).where(Bed.id == from_bed).values(status="free"))

    fired = _fire_once_after(monkeypatch, "assert_obj_org_writable", rival)

    resp = client.post(
        f"/api/inpatient/admissions/{aid}/transfer",
        json={"ward_id": ward["ward"]["id"], "bed_id": my_bed},
        headers=doctor,
    )
    assert fired, "侧信道没触发，这条用例什么也没证明"
    assert resp.status_code == 409
    assert resp.json()["detail"] == "仅在院患者可转科/转床"

    db = SessionLocal()
    try:
        assert db.get(Bed, my_bed).status == "free", "抢输者占上的床没退回来，就是漏床"
    finally:
        db.close()


@pytest.mark.parametrize("target", ["missing", "other_ward"])
def test_转床到不存在或不属于本病区的床仍是404(client, admin, ward, doctor, target):
    """目标床校验前移到比较交换之前，是因为 `admissions.bed_id` 在 PG 上是一条
    真外键：不先查一下，转到不存在的床会在比较交换处抛 IntegrityError，
    由 404 变成没人接的 500（SQLite 不校验外键，看不出来）。文案与位置都不许变。
    """
    _, admission = _admit(client, admin, ward, f"迁移戊{target}",
                          "330281198506060123" if target == "missing" else "330281198506060139",
                          7 if target == "missing" else 9)
    bed_id = 999999 if target == "missing" else ward["other_bed"]["id"]

    resp = client.post(
        f"/api/inpatient/admissions/{admission['id']}/transfer",
        json={"ward_id": ward["ward"]["id"], "bed_id": bed_id},
        headers=doctor,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "床位不存在或不属于该病区"

    unchanged = client.get(
        f"/api/inpatient/admissions?patient_id={admission['patient_id']}", headers=admin
    ).json()[0]
    assert unchanged["status"] == "admitted"
    assert unchanged["bed_id"] == admission["bed_id"], "报错的转床不该动住院记录"
    assert _bed_status(client, admin, admission["bed_id"], ward["ward"]["id"]) == "occupied"


# ================================================================ 防拆卸静态钉


def _router_sources():
    for path in sorted(ROUTERS_DIR.rglob("*.py")):
        yield path, path.read_text(encoding="utf-8")


def _code_of(func) -> str:
    """函数源码去掉注释与 docstring 后的规范形式：静态钉必须钉在**代码**上。

    这是写这一档时自己踩出来的：第一版钉的是 `"synchronize_session=False" in 源码`，
    而这句话恰好也写在 `_mark_discharged` 的 docstring 里——把
    `.execution_options(...)` 整个删掉，钉子照样绿。同理 integration 里那句
    "闸门是 `_mark_discharged`……"的注释也能替真调用背书。
    `ast.unparse` 天生丢掉注释，再摘掉 docstring，剩下的才是真在跑的东西
    （字符串字面量会被规范成单引号，断言按这个口径写）。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        head = node.body[0] if node.body else None
        if (
            isinstance(head, ast.Expr)
            and isinstance(head.value, ast.Constant)
            and isinstance(head.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_路由层不得再有直接置discharged的赋值():
    """`admission.status = "discharged"` 是这个洞的原始形状：判定停在 Python 侧，
    写入不带任何条件。两个出院入口都改走闸门之后，路由层不该再出现这一句。
    """
    offenders = []
    for path, source in _router_sources():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Assign):
                continue
            if not (isinstance(node.value, ast.Constant) and node.value.value == "discharged"):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "status":
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        "以下位置又把出院状态写成了无条件赋值（并发下两路都会写成功）："
        f"{offenders}；请走 inpatient._mark_discharged 的条件 UPDATE。"
    )


@pytest.mark.parametrize(
    "module_name,func_name",
    [("app.routers.inpatient", "discharge_admission"), ("app.routers.integration", "_do_hl7v2_adt")],
)
def test_两个出院入口都必须过闸门(module_name, func_name):
    """A03 镜像与平台端点写的是同一行。少一个走闸门，两个入口之间的竞争就还在。"""
    import importlib

    code = _code_of(getattr(importlib.import_module(module_name), func_name))
    assert "_mark_discharged(db" in code, f"{func_name} 绕开了出院闸门"
    assert "db.rollback()" in code, f"{func_name} 抢输时没回滚，会一路攥着写锁"


def test_出院闸门必须关掉ORM回写同步():
    """ORM 版 UPDATE 默认会把 SET 值评估到会话内对象上——连 rowcount=0 的抢输方
    也会被翻成 discharged（它手上那份旧属性恰好满足 WHERE）。关掉它是正确性的
    一部分，不是风格问题。
    """
    code = _code_of(inpatient_router._mark_discharged)
    assert ".execution_options(synchronize_session=False)" in code
    assert "Admission.status == 'admitted'" in code, "闸门的条件被放宽了"


def test_转床的目标床校验必须排在比较交换之前():
    """顺序反了在 PG 上就是 404 变 500（`admissions.bed_id` 是真外键），
    而 SQLite 不校验外键，单测看不出来——所以这一条只能静态钉。
    """
    code = _code_of(inpatient_router.transfer_admission)
    assert "db.get(Bed" in code, "目标床存在性校验没了"
    assert code.index("db.get(Bed") < code.index("update(Admission")
    assert "_occupy_bed(db" in code
    assert code.index("update(Admission") < code.index("_occupy_bed(db"), (
        "比较交换必须排在占床之前：出院先锁 admission 行，转床若先锁床，两者并发会在 PG 上形成锁环"
    )
    assert "Admission.bed_id == old_bed_id" in code, "比较交换丢了「从我读到的那张床上换走」这一半"
