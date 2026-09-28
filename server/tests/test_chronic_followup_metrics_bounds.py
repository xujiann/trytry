"""慢病随访 `metrics` 里与列同名的指标按列的同一口径、分级用到的指标不收负数（P1-214，第十八批「数值入参的符号与业务
上下界」扫描 V3-1）。

P1-101 给慢病随访的收缩压 / 舒张压 / 空腹血糖三列加了界：0 正是血压计测量失败时最常回的值，0 / 负数按「越高越危」
比阈值永远判成控制良好，3 级高危一次就降成 1 级、上转建议消失。可分级取值是「列为空就取 metrics 同名键」
（`chronic._metric_value`），metrics 是 `dict[str, FiniteFloat]`、没有任何界——`{"metrics": {"sbp": 0, "dbp": 0}}`
照收，同一个降级原样发生（闸门按字段名看请求模型，看不见字典里的键）。只经 metrics 取值的六个病种的量表分 / 次数
（CAT、心绞痛次数、mRS、依从性、ECOG、漏服次数）收负数：COPD 的 CAT -25、结核漏服 -5 同样把 3 级降成 1 级。

修法：metrics 里与列同名的键用请求模型自己那三列的声明再校验一遍（同一份界，不另抄数字）；本病种分级规则用到的
指标不收负数（量表分、次数没有负的；0 照收——CAT 0、mRS 0 都是真实值）；分级之外的自定义指标不管。各量表的
上界（CAT 0-40、mRS 0-6、依从性 0-10……）写在哪属口径，另行登记。
"""
import pytest

from app.database import SessionLocal
from app.models import ChronicPatient


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1214 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P1214 患者{i}", "id_card": f"33028119750101{1214 + i:04d}"}).json()["id"] for i in range(3)]
    return {"org": org, "patients": patients}


def _chronic(client, admin, world, index, disease):
    resp = client.post("/api/chronic", headers=admin, json={
        "patient_id": world["patients"][index], "disease": disease, "managed_by_org_id": world["org"]})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _followup(client, admin, chronic_id, body):
    return client.post(f"/api/chronic/{chronic_id}/followups", headers=admin, json=body)


def _level(chronic_id):
    with SessionLocal() as db:
        return db.get(ChronicPatient, chronic_id).level


def test_metrics里的同名血压按列的口径_0不能把高危降成控制良好(client, admin, world):
    cid = _chronic(client, admin, world, 0, "hypertension")
    first = _followup(client, admin, cid, {"sbp": 185, "dbp": 112})
    assert first.status_code == 201 and first.json()["level"] == 3, first.text
    for metrics in ({"sbp": 0, "dbp": 0}, {"sbp": -1, "dbp": 80}, {"sbp": 3000, "dbp": 90}, {"glucose": 0}):
        resp = _followup(client, admin, cid, {"metrics": metrics})
        assert resp.status_code == 422, (metrics, resp.text)   # 修前 0/0 → 201、降成 1 级、上转建议消失
    assert _level(cid) == 3
    # 同名键取值合法照旧用得上：列空着就按 metrics 里的定级
    ok = _followup(client, admin, cid, {"metrics": {"sbp": 150, "dbp": 95}})
    assert ok.status_code == 201 and ok.json()["level"] == 2, ok.text


def test_分级用到的量表分不收负数_0照收(client, admin, world):
    cid = _chronic(client, admin, world, 1, "copd")
    assert _followup(client, admin, cid, {"metrics": {"cat_score": 25}}).json()["level"] == 3
    resp = _followup(client, admin, cid, {"metrics": {"cat_score": -25}})
    assert resp.status_code == 422, resp.text   # 修前 201、降成 1 级
    assert "cat_score" in resp.json()["detail"]
    assert _level(cid) == 3
    zero = _followup(client, admin, cid, {"metrics": {"cat_score": 0}})   # CAT 0 分是真实值
    assert zero.status_code == 201 and zero.json()["level"] == 1, zero.text


def test_分级之外的自定义指标不管(client, admin, world):
    cid = _chronic(client, admin, world, 2, "hypertension")
    resp = _followup(client, admin, cid, {"sbp": 128, "dbp": 80, "metrics": {"weight_change_kg": -2}})
    assert resp.status_code == 201, resp.text
    assert resp.json()["followup"]["metrics"] == {"weight_change_kg": -2}
