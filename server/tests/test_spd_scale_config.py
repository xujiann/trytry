"""量表配置写坏了照样建得成、发得布，谁来作答都 500——居民扫码自查也一样（P2-80）。

`score_scale` 按题目选项的分值累加、再落到 `scoring.ranges` 给风险等级。建 / 改 / 发布量表原先只查题目 key
不重复：选项写成字符串（`["是", "否"]`）、分值写成文字、选项或评分分段不是列表，建量表 201、发布 200，作答时
`AttributeError` / `ValueError`——医护评估、筛查登记、居民自查三个入口都 500。题目没写 key 的，作答永远计不进分。
界面不建量表，写得进去的是接口调用方。

修法：建 / 改 / 发布量表查配置（422）；修前已发布的坏量表作答时 422 说清楚、不 500。分值只要读得成数就照收
（`"3"` 修前就照常计分）。
"""
import pytest

B = "/api/spd"
GOOD_ITEM = {"key": "q1", "type": "single", "options": [{"label": "是", "score": 3}, {"label": "否", "score": 0}]}
BAD = [
    ([{"key": "q1", "type": "single", "options": ["是", "否"]}], {},
     "题目 q1 的选项要写成 [{label, score}] 这样的列表"),
    ([{"key": "q1", "type": "single", "options": "是/否"}], {}, "题目 q1 的选项要写成 [{label, score}] 这样的列表"),
    ([{"key": "q1", "type": "single", "options": [{"label": "是", "score": "高"}]}], {}, "题目 q1 的选项分值必须是数：'高'"),
    ([GOOD_ITEM], {"ranges": ["0-5:低危"]}, "评分分段（scoring.ranges）要写成 [{min, max, risk, advice}] 这样的列表"),
    ([GOOD_ITEM], {"ranges": {"min": 0}}, "评分分段（scoring.ranges）要写成 [{min, max, risk, advice}] 这样的列表"),
    ([{"type": "single", "options": [{"label": "是", "score": 1}]}], {}, "每道题都要有 key"),
]
IDS = ["选项是字符串", "选项不是列表", "分值是文字", "分段是字符串", "分段不是列表", "题目没有key"]


def _scale(client, admin, code, items, scoring=None):
    return client.post(f"{B}/scales", headers=admin,
                       json={"code": code, "name": f"{code} 量表", "items": items, "scoring": scoring or {}})


@pytest.mark.parametrize(("items", "scoring", "detail"), BAD, ids=IDS)
def test_建量表_作答会500或计不进分的配置一律422(client, admin, items, scoring, detail):
    """修前六种写法建量表全部 201、发布 200；前五种作答 500，没 key 的题永远计不进分。"""
    resp = _scale(client, admin, "P280_BAD", items, scoring)
    assert resp.status_code == 422 and resp.json() == {"detail": f"量表配置非法：{detail}"}, resp.text[:300]


def test_分值写成数字字符串照收_照常计分(client, admin):
    """`score_scale` 本就按 float() 读分值，写成 "3" 的修前就照常计分——不因为这次校验变成 422。"""
    items = [{"key": "q1", "type": "single", "options": [{"label": "是", "score": "3"}]}]
    created = _scale(client, admin, "P280_STR", items, {"ranges": [{"min": 3, "risk": "high", "advice": "复诊"}]})
    assert created.status_code == 201, created.text
    assert client.post(f"{B}/scales/{created.json()['id']}/publish", headers=admin).status_code == 200
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P280 患者", "id_card": "330106197001011792", "gender": "女", "birth_date": "1970-01-01"}).json()["id"]
    done = client.post(f"{B}/assessments", headers=admin,
                       json={"patient_id": patient, "scale_code": "P280_STR", "answers": {"q1": "是"}})
    assert done.status_code == 201, done.text
    assert (done.json()["score"], done.json()["risk_level"]) == (3, "high")


def test_改草稿也查_题目与评分合起来(client, admin):
    created = _scale(client, admin, "P280_EDIT", [GOOD_ITEM])
    assert created.status_code == 201, created.text
    url = f"{B}/scales/{created.json()['id']}"
    resp = client.patch(url, headers=admin, json={"items": [{"key": "q1", "options": ["是"]}]})
    assert resp.status_code == 422, resp.text
    resp = client.patch(url, headers=admin, json={"scoring": {"ranges": "0-5"}})
    assert resp.status_code == 422, resp.text
    resp = client.patch(url, headers=admin, json={"name": "P280 改名"})   # 不动题目与评分不查
    assert resp.status_code == 200, resp.text


@pytest.fixture(scope="module")
def legacy(client, admin):
    """修前落库的坏量表：一份草稿、一份已发布（选项写成字符串），外加作答用的患者、病种与居民。"""
    from app.config import settings
    from app.database import SessionLocal
    from app.models import Patient, ResidentAccount
    from app.spd.models import SpdScale

    bad_items = [{"key": "q1", "type": "single", "options": ["是", "否"]}]
    with SessionLocal() as db:
        draft = SpdScale(code="P280_DRAFT", name="存量坏草稿", items=bad_items, scoring={}, status="draft")
        published = SpdScale(code="P280_PUB", name="存量坏量表", items=bad_items, scoring={}, status="published")
        me = Patient(ehc_no="EHC-P280-ME", name="P280 居民", id_card="330106198001011803", gender="女",
                     birth_date="1980-01-01", phone="13912801280")
        db.add_all([draft, published, me])
        db.flush()
        db.add(ResidentAccount(phone="13912801280", patient_id=me.id, nickname="P280", wechat_openid="",
                               status="active"))
        db.commit()
        ids = {"draft": draft.id, "patient": me.id}
    r = client.post(f"{B}/programs", headers=admin, json={"code": "P280_PG", "name": "P280 病种", "category": "chronic"})
    assert r.status_code == 201, r.text
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code",
                           json={"phone": "13912801280", "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login",
                            json={"phone": "13912801280", "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    return {**ids, "resident": {"Authorization": f"Bearer {token}"}}


USE_DETAIL = "量表配置有误（题目 q1 的选项要写成 [{label, score}] 这样的列表），暂不能作答，请联系管理员修正"


def test_存量坏草稿_改好再发布(client, admin, legacy):
    resp = client.post(f"{B}/scales/{legacy['draft']}/publish", headers=admin)
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": "量表配置非法：题目 q1 的选项要写成 [{label, score}] 这样的列表"}


@pytest.mark.parametrize("entry", ["医护评估", "筛查登记", "居民自查"])
def test_存量已发布的坏量表_三个作答入口都422说清楚_不再500(client, admin, legacy, entry):
    answers = {"q1": "是"}
    if entry == "医护评估":
        resp = client.post(f"{B}/assessments", headers=admin,
                           json={"patient_id": legacy["patient"], "scale_code": "P280_PUB", "answers": answers})
    elif entry == "筛查登记":
        resp = client.post(f"{B}/screenings", headers=admin, json={
            "patient_id": legacy["patient"], "program_code": "P280_PG", "scale_code": "P280_PUB", "answers": answers})
    else:
        resp = client.post("/api/portal/spd/screenings", headers=legacy["resident"],
                           json={"program_code": "P280_PG", "scale_code": "P280_PUB", "answers": answers})
    assert resp.status_code == 422 and resp.json() == {"detail": USE_DETAIL}, resp.text[:300]
