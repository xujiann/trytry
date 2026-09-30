"""质控规则 QC013 与病种目录对「冠心病 / 脑卒中随访该录什么」各写一份：按目录录的被质控点名，按质控录的不定级
（P2-1049，第三十批「临床判定的阈值与所引依据」扫描 D3-6）。

QC013 的规则配置手抄了一份「病种 → 要录的指标」：冠心病、脑卒中写的是收缩压、舒张压；病种目录（页面自称「分级规则与随访
周期的唯一数据源」）按每周心绞痛发作次数、改良 Rankin 评分定级，移动端这两个病种根本没有血压格。于是只录心绞痛 4 次 / 周（3 级）
的冠心病随访被点名「缺少指标：sbp、dbp」，录 mRS 4 的脑卒中同样；只录血压 150/92 的冠心病随访过了质控、却定不了级。质控看的
还只是固定列，不看 metrics 里的指标。

修法：要录哪些指标以目录的分级规则为准，取值与定级同一个读法（同名列，再取 metrics），require_all=false 的记了任一项即可；
目录没写分级指标的病种退回规则配置里的那份。冠心病、脑卒中要不要另外要求录血压不在这里定。
"""
import itertools

import pytest

_CARDS = itertools.count(1)


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21049 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _followup(client, admin, org, disease, body):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21049 患者", "id_card": f"33010619600101{next(_CARDS) + 1049:04d}", "gender": "男",
        "birth_date": "1960-01-01"}).json()["id"]
    chronic = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient, "disease": disease, "managed_by_org_id": org})
    assert chronic.status_code == 201, chronic.text
    got = client.post(f"/api/chronic/{chronic.json()['id']}/followups", headers=admin, json=body)
    assert got.status_code == 201, got.text
    return got.json()["followup"]["id"], got.json()["level"]


def _qc013(client, admin):
    rows = client.get("/api/dataquality/run?rule_code=QC013&limit=1000", headers=admin).json()["items"]
    return {row["record_id"]: row["message"] for row in rows}


def test_按目录录的冠心病脑卒中随访不被点名_只录血压的点名缺目录指标(client, admin, org):
    chd, chd_level = _followup(client, admin, org, "chd", {"metrics": {"angina_weekly": 4}})
    stroke, _ = _followup(client, admin, org, "stroke", {"metrics": {"mrs_score": 4}})
    bp_only, bp_level = _followup(client, admin, org, "chd", {"sbp": 150, "dbp": 92})
    htn, _ = _followup(client, admin, org, "hypertension", {"sbp": 135, "dbp": 85})
    assert (chd_level, bp_level) == (3, 1)   # 目录按心绞痛次数定级；只录血压的定不了级
    hits = _qc013(client, admin)
    assert chd not in hits and stroke not in hits   # 修前点名「缺少指标：sbp、dbp」
    assert hits.get(bp_only) == "chd 随访缺少指标：angina_weekly"   # 修前过了质控
    assert htn not in hits   # 目录与原配置一致的病种照旧
