"""筛查登记不看量表是不是这个病种的：用糖尿病的问卷给高血压筛查打分，答高了就进高血压的疑似目标池（P2-98）。

筛查登记（`POST /api/spd/screenings`）按量表评分定风险等级，高危即判「疑似」、写进这个病种的目标池。量表挂在病种上
（`program_code`，空串是通用量表），接口只查量表在不在、发没发布，不看它是不是这个病种的。管理端「筛查登记」的量表
下拉又列的是全部病种已发布的筛查量表、跟病种下拉不联动——病种选高血压、量表点到「糖尿病高危筛查问卷」，年龄、家族史、
多饮多尿几题答「是」，201：风险高危、结论疑似，这位患者进了**高血压**的疑似目标池，复核的人看到的是一份跟高血压
无关的问卷。居民自查接口同一个缺口（居民端页面按量表带病种，接口调用方可以对不上）。与 P2-95（路径模板的病种须是
档案的病种）同一口径。

修法：筛查用的量表须是这个病种的或通用的（空串），不一致 422、什么都不写；管理端的量表下拉随病种联动。
"""
import pytest

B = "/api/spd"
DIABETES_HIGH = {"age": "是", "family": "是", "overweight": "是", "symptom": "是"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P298 筛查卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P298 居民", "id_card": "330127196901010298", "phone": "13929800298"}).json()
    return {"org": org, "patient": patient}


def _candidates(patient_id, program_code):
    from app.database import SessionLocal
    from app.spd.models import SpdCandidate

    with SessionLocal() as db:
        return db.query(SpdCandidate).filter_by(patient_id=patient_id, program_code=program_code).count()


def test_别的病种的筛查量表_登记422_不进目标池(client, admin, world):
    resp = client.post(f"{B}/screenings", headers=admin, json={
        "patient_id": world["patient"]["id"], "program_code": "hypertension", "scale_code": "scr_diabetes",
        "source": "opportunistic", "org_id": world["org"], "answers": DIABETES_HIGH})
    assert resp.status_code == 422, resp.text   # 修前 201：高危、疑似
    assert resp.json() == {"detail": "筛查量表的病种与筛查病种不一致"}
    assert _candidates(world["patient"]["id"], "hypertension") == 0   # 修前进了高血压的疑似目标池


def test_本病种的量表照常登记(client, admin, world):
    resp = client.post(f"{B}/screenings", headers=admin, json={
        "patient_id": world["patient"]["id"], "program_code": "diabetes", "scale_code": "scr_diabetes",
        "source": "opportunistic", "org_id": world["org"], "answers": DIABETES_HIGH})
    assert resp.status_code == 201 and resp.json()["result"] == "suspect", resp.text


def test_居民自查接口同一口径(client, world):
    phone = world["patient"]["phone"]
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
    login = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
    assert login.status_code == 200, login.text
    ph = {"Authorization": f"Bearer {login.json()['access_token']}"}
    bound = client.post("/api/portal/auth/realname", headers=ph, json={
        "name": world["patient"]["name"], "id_card": world["patient"]["id_card"]})
    # 同一手机号登录即自动认上本人档案（已绑定 409），没认上的才要实名绑定
    assert bound.status_code == 200 or bound.json() == {"detail": "该账户已完成实名绑定"}, bound.text
    resp = client.post("/api/portal/spd/screenings", headers=ph, json={
        "program_code": "hypertension", "scale_code": "scr_diabetes", "answers": DIABETES_HIGH})
    assert resp.status_code == 422 and resp.json() == {"detail": "筛查量表的病种与筛查病种不一致"}, resp.text
    ok = client.post("/api/portal/spd/screenings", headers=ph, json={
        "program_code": "diabetes", "scale_code": "scr_diabetes", "answers": DIABETES_HIGH})
    assert ok.status_code == 201, ok.text


def test_评估写了病种的_量表须是这个病种的_档案风险不被别的病种改写(client, admin, world):
    """评估同一口径：写了病种、量表却是别的病种的，原先照样按那张量表评分、回写这个病种档案的风险等级，高危还自动
    派干预与复诊。管理端评估页不带病种（取量表自己的），接口调用方写得进。"""
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": world["patient"]["id"], "program_code": "hypertension", "org_id": world["org"],
        "risk_level": "low"})
    assert enrollment.status_code == 201, enrollment.text
    resp = client.post(f"{B}/assessments", headers=admin, json={
        "patient_id": world["patient"]["id"], "program_code": "hypertension", "scale_code": "scr_diabetes",
        "answers": DIABETES_HIGH})
    assert resp.status_code == 422, resp.text   # 修前 201：高血压档案被改成高危
    assert resp.json() == {"detail": "评估量表的病种与评估病种不一致"}
    detail = client.get(f"{B}/enrollments/{enrollment.json()['id']}", headers=admin).json()
    assert detail["risk_level"] == "low"
