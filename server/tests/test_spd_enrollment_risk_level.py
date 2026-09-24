"""改档的风险分层与建档同一个取值范围（P2-50）。

建档（`EnrollIn.risk_level`）只收 `low / mid / high / very_high`；改档（`EnrollUpdate.risk_level`）
是裸 `str | None`，什么都收。下游统计全按这四个值数：工作台「高危在管」、考核里的高危人数
都是 `risk_level in ("high", "very_high")`——改档写进一个「HIGH」或「高危」，这个人就从高危统计里
悄悄消失，列表上的风险标签也只能原样显示一个不认识的码。

界面上的「调整」弹窗是四选一的下拉，走得到这里的是直接调接口的一方（对接、脚本），故列 P2。
修法：改档的风险分层与建档同一个 pattern；四个合法值照常可改（特征化用例钉住）。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def enrollment(client, admin):
    org = client.post("/api/organizations", json={"name": "风险分层卫生院", "org_type": "township",
                                                  "level": "township"}, headers=admin).json()["id"]
    r = client.post(f"{B}/programs", json={"code": "p250_prog", "name": "风险分层病种", "category": "chronic"},
                    headers=admin)
    assert r.status_code == 201, r.text
    pid = client.post("/api/patients", json={"name": "风险分层患者", "id_card": "330190198001010250",
                                             "gender": "男", "birth_date": "1980-01-01"}, headers=admin).json()["id"]
    r = client.post(f"{B}/enrollments", json={"patient_id": pid, "program_code": "p250_prog", "org_id": org},
                    headers=admin)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.parametrize("value", ["low", "mid", "high", "very_high"])
def test_特征化_四个合法值照常可改(client, admin, enrollment, value):
    r = client.patch(f"{B}/enrollments/{enrollment}", json={"risk_level": value}, headers=admin)
    assert r.status_code == 200 and r.json()["risk_level"] == value, r.text


@pytest.mark.parametrize("value", ["HIGH", "高危", "", "critical"])
def test_改档写进不认识的风险分层_422(client, admin, enrollment, value):
    before = client.patch(f"{B}/enrollments/{enrollment}", json={"risk_level": "high"}, headers=admin)
    assert before.status_code == 200, before.text
    r = client.patch(f"{B}/enrollments/{enrollment}", json={"risk_level": value}, headers=admin)
    assert r.status_code == 422, (value, r.status_code, r.text[:200])
    # 没改成：仍是高危，高危统计里还数得到它
    after = client.patch(f"{B}/enrollments/{enrollment}", json={}, headers=admin)
    assert after.json()["risk_level"] == "high"
