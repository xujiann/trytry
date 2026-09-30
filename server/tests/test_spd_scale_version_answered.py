"""按旧版量表的题作答、按新版评分（P2-921，第二十五批「配置改动的生效时点」扫描 J1-2）。

筛查登记、量表评估、居民自查都按量表编码取「最新发布」的那一版评分，还把那一版记进评估记录；页面按载入时的
目录出题、提交只送编码。页面打开之后同编码发布了新版（题目、分值、分段都可能换了），这一页上照旧弹出旧版的题目，
提交后按新版评分——按 v1 作答「是」本该 5 分高危，按 v2 的分值表查不到这个选项、评成 0 分低危，评估记录还写成 v2。

修法：三处入参加可选的 `scale_id`（作答的那一版），不是现行发布版就 409、请刷新后重答；三个页面都带上。
不带 `scale_id` 的旧调用照旧按现行版评分。
"""
from pathlib import Path

import pytest

from conftest import login

B = "/api/spd"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PATIENT = {"name": "P2921 量表居民", "id_card": "330106196509210922", "gender": "女",
           "birth_date": "1965-09-21", "phone": "13900009210"}
CODE = "scr_p2921"
V1_ITEMS = [{"key": "q1", "title": "近一月头晕", "type": "single",
             "options": [{"label": "否", "score": 0}, {"label": "是", "score": 5}]}]
V2_ITEMS = [{"key": "q1", "title": "近一周头晕次数", "type": "single",
             "options": [{"label": "没有", "score": 0}, {"label": "三次以上", "score": 5}]}]
SCORING = {"ranges": [{"min": 0, "max": 2, "risk": "low", "advice": "保持"},
                      {"min": 3, "max": None, "risk": "high", "advice": "尽快复核"}]}
STALE = "不是作答时的那一版"


@pytest.fixture(scope="module")
def h(client):
    return login(client, "admin", "admin123")


def _publish(client, h, version, items):
    made = client.post(f"{B}/scales", headers=h, json={
        "code": CODE, "name": "P2921 头晕筛查", "category": "screen", "program_code": "hypertension",
        "version": version, "items": items, "scoring": SCORING})
    assert made.status_code == 201, made.text
    assert client.post(f"{B}/scales/{made.json()['id']}/publish", headers=h).status_code == 200
    return made.json()["id"]


@pytest.fixture(scope="module")
def base(client, h):
    org = client.post("/api/organizations", headers=h, json={
        "name": "P2921 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=h, json=PATIENT)
    assert patient.status_code in (200, 201), patient.text
    v1 = _publish(client, h, "v1", V1_ITEMS)   # 页面在这之后载入、按 v1 出题
    v2 = _publish(client, h, "v2", V2_ITEMS)   # 页面开着的时候发布了新版
    return {"org": org, "patient": patient.json()["id"], "v1": v1, "v2": v2}


@pytest.fixture(scope="module")
def ph(client, base):
    """居民令牌：短信验证码登录 + 实名绑定（与既有居民端用例同一取法）。"""
    code = client.post("/api/portal/auth/sms/code", json={"phone": PATIENT["phone"]}).json()["debug_code"]
    resp = client.post("/api/portal/auth/sms/login", json={"phone": PATIENT["phone"], "code": code})
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    client.post("/api/portal/auth/realname", headers=headers,
                json={"name": PATIENT["name"], "id_card": PATIENT["id_card"]})
    return headers


def _screen(client, h, base, **extra):
    return client.post(f"{B}/screenings", headers=h, json={
        "patient_id": base["patient"], "program_code": "hypertension", "source": "opportunistic",
        "org_id": base["org"], "scale_code": CODE, **extra})


def test_筛查登记_按旧版作答的_409_不按新版评分(client, h, base):
    before = len(client.get(f"{B}/screenings?patient_id={base['patient']}", headers=h).json())
    stale = _screen(client, h, base, scale_id=base["v1"], answers={"q1": "是"})
    assert stale.status_code == 409, stale.text   # 修前 201：按 v2 评成 0 分低危
    assert STALE in stale.json()["detail"] and "v2" in stale.json()["detail"]
    assert len(client.get(f"{B}/screenings?patient_id={base['patient']}", headers=h).json()) == before

    fresh = _screen(client, h, base, scale_id=base["v2"], answers={"q1": "三次以上"})
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["risk_level"] == "high"


def test_量表评估_按旧版作答的_409_记录不写成新版(client, h, base):
    stale = client.post(f"{B}/assessments", headers=h, json={
        "patient_id": base["patient"], "scale_code": CODE, "scale_id": base["v1"], "answers": {"q1": "是"}})
    assert stale.status_code == 409, stale.text   # 修前 201：0 分低危、scale_version 记成 v2
    assert STALE in stale.json()["detail"]

    fresh = client.post(f"{B}/assessments", headers=h, json={
        "patient_id": base["patient"], "scale_code": CODE, "scale_id": base["v2"], "answers": {"q1": "三次以上"}})
    assert fresh.status_code == 201, fresh.text
    assert (fresh.json()["score"], fresh.json()["risk_level"]) == (5, "high")


def test_居民自查_按旧版作答的_409(client, ph, base):
    stale = client.post("/api/portal/spd/screenings", headers=ph, json={
        "program_code": "hypertension", "scale_code": CODE, "scale_id": base["v1"], "answers": {"q1": "是"}})
    assert stale.status_code == 409, stale.text   # 修前 201：告诉居民「低危」、不提示申请服务
    assert STALE in stale.json()["detail"]

    fresh = client.post("/api/portal/spd/screenings", headers=ph, json={
        "program_code": "hypertension", "scale_code": CODE, "scale_id": base["v2"], "answers": {"q1": "三次以上"}})
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["risk_level"] == "high"


def test_不带作答版本的旧调用照旧按现行版评分(client, h, base):
    legacy = _screen(client, h, base, answers={"q1": "三次以上"})
    assert legacy.status_code == 201, legacy.text
    assert legacy.json()["risk_level"] == "high"
    assessed = client.post(f"{B}/assessments", headers=h, json={
        "patient_id": base["patient"], "scale_code": CODE, "answers": {"q1": "三次以上"}})
    assert assessed.status_code == 201, assessed.text


def test_三个作答页面都带上作答的那一版():
    spd = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    screen = spd[spd.index('$("#spd-screen-form").onsubmit'):]
    screen = screen[:screen.index('postAction("/api/spd/screenings"')]
    assert "body.scale_id = scale.id" in screen, screen   # 修前只送 scale_code
    assess = spd[spd.index('$("#spd-assess-form").onsubmit'):]
    assess = assess[:assess.index("setMsg(\"#spd-assess-msg\",\n")]
    assert "scale_id: scale.id" in assess, assess
    m = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    submit = m[m.index('$("#spd-screen-submit").addEventListener'):]
    submit = submit[:submit.index('"/api/portal/spd/screenings"')]
    assert "scale_id: scale.id" in submit, submit
