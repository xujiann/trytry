"""P0-3 的回归网：同一份量表答案，医生录入与居民自查必须判出同一个结论。

修之前是两套口径：医生侧 `risk == "high"` 才算疑似（`population.py`），
居民侧 `risk_level in ("mid","high")` 就算疑似并允许申请服务（`portal.py`）。
于是同一个人、同一份问卷、同样的答案——自己在手机上做是"疑似，可申请管理服务"，
医生代录进系统是"未见异常"。两个数字都会进报表：目标池人数因此取决于**是谁录的**。

口径现在只有一处：`spd/rules.py::is_suspect_risk`（取 mid+，理由写在那里）。
本用例拿**同一份答案**走两个入口，断言两边 `result` 一致——这是那类"各写各的"
缺陷唯一测得住的形态：只测一个入口，两边分叉时照样全绿。
"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def h(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, h):
    org = client.post(
        "/api/organizations",
        json={"name": "口径一致性卫生院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "口径患者", "id_card": "330288198803030044", "gender": "男",
              "birth_date": "1988-03-03", "phone": "13900004444"},
        headers=h,
    ).json()
    # 三档量表：0-2 低危 / 3-5 中危 / 6+ 高危。中危档正是两套口径分叉的地方
    scale = client.post(
        "/api/spd/scales",
        json={
            "code": "scr_threshold", "name": "口径一致性筛查量表", "category": "screen",
            "program_code": "hypertension",
            "items": [
                {"key": f"q{i}", "title": f"危险因素{i}", "type": "single",
                 "options": [{"label": "否", "score": 0}, {"label": "是", "score": 2}]}
                for i in range(1, 4)
            ],
            "scoring": {"ranges": [
                {"min": 0, "max": 2, "risk": "low", "advice": "保持健康生活方式"},
                {"min": 3, "max": 5, "risk": "mid", "advice": "建议复核血压"},
                {"min": 6, "max": None, "risk": "high", "advice": "建议尽快到基层机构评估"},
            ]},
        },
        headers=h,
    ).json()
    client.post(f"/api/spd/scales/{scale['id']}/publish", headers=h)
    return {"org": org, "patient": patient, "scale": scale}


@pytest.fixture(scope="module")
def ph(client, base):
    """居民令牌：短信验证码登录 + 实名绑定（与既有居民端用例同一取法）。"""
    phone = base["patient"]["phone"]
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
    login = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    client.post(
        "/api/portal/auth/realname",
        json={"name": base["patient"]["name"], "id_card": base["patient"]["id_card"]},
        headers=headers,
    )
    return headers


#: (答案, 期望等级, 期望判定)。中危那条是修复前会分叉的用例。
CASES = [
    pytest.param({"q1": "否", "q2": "否", "q3": "否"}, "low", "normal", id="低危→未见异常"),
    pytest.param({"q1": "是", "q2": "是", "q3": "否"}, "mid", "suspect", id="中危→疑似"),
    pytest.param({"q1": "是", "q2": "是", "q3": "是"}, "high", "suspect", id="高危→疑似"),
]


@pytest.mark.parametrize("answers,expect_risk,expect_result", CASES)
def test_两个入口对同一份答案判出同一结论(client, h, ph, base, answers, expect_risk, expect_result):
    doctor_side = client.post(
        "/api/spd/screenings",
        json={"patient_id": base["patient"]["id"], "program_code": "hypertension",
              "source": "opportunistic", "org_id": base["org"]["id"],
              "scale_code": "scr_threshold", "answers": answers},
        headers=h,
    )
    assert doctor_side.status_code == 201, doctor_side.text
    doctor = doctor_side.json()

    resident_side = client.post(
        "/api/portal/spd/screenings",
        json={"program_code": "hypertension", "scale_code": "scr_threshold",
              "answers": answers},
        headers=ph,
    )
    assert resident_side.status_code == 201, resident_side.text
    resident = resident_side.json()

    assert doctor["risk_level"] == resident["risk_level"] == expect_risk
    assert doctor["result"] == resident["result"] == expect_result, (
        f"同一份答案两个入口判定不一致：医生侧 {doctor['result']}、"
        f"居民侧 {resident['result']}——口径又分叉了"
    )


def test_中危可申请服务与疑似判定同源(client, ph, base):
    """`can_apply` 不能自己另判一次：能不能申请服务与是不是疑似必须同一个门槛。"""
    resp = client.post(
        "/api/portal/spd/screenings",
        json={"program_code": "hypertension", "scale_code": "scr_threshold",
              "answers": {"q1": "是", "q2": "是", "q3": "否"}},
        headers=ph,
    ).json()
    assert resp["result"] == "suspect" and resp["can_apply"] is True


def test_口径只写在一处():
    """两个路由都不许再出现自己的等级清单——那正是分叉的成因。"""
    import pathlib
    import re

    routers_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "spd" / "routers"
    for name in ("population.py", "portal.py"):
        # 锚在 __file__ 而非 CWD——隔离工作目录跑单测（多 agent 并行的既定做法）不该假红
        src = (routers_dir / name).read_text(encoding="utf-8")
        hits = re.findall(r'risk_level["\']?\s*(?:in|==)\s*\(?["\'](?:mid|high)', src)
        assert not hits, (
            f"{name} 里又出现了自己判风险等级的写法（{hits}）——"
            "请统一走 rules.is_suspect_risk"
        )
