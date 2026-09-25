"""公卫随访采集器一次只看回溯窗口里最早的 500 条：窗口里多于 500 条随访，其余的体征永远同步不进慢专病（P2-94）。

`collect_publichealth` 把平台公卫慢病随访（`followups`）的血压 / 血糖同步成慢专病监测值，回溯窗口是同步周期的 4 倍
（「比同步周期宽一倍以上，一次漏跑补得回来」）。取数却是「窗口内按 id 升序取前 500 条」，再在 Python 里按 `source_ref`
判重：窗口里有 800 条时，每一轮取到的都是同一批最早的 500 条——头一轮落库，之后几轮全被判重跳过——后 300 条一轮都
轮不到，窗口移过去就永远出了窗口。不报错、同步日志照写「成功」。周期 60 分钟即 4 小时里 500 条随访（集中录入日、
批量补录），周期拉到一天（上限 1440 分钟）即 4 天里 500 条——县域的公卫随访量两种都撞得上。

修法：按 id 翻页把整个窗口取完（每页 500 条），判重只查这一页的来源键（原先每轮把全部监测值的来源键读进内存，
随数据量线性长）。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P294 公卫中心", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P294 患者", "id_card": "330127196808080294"}).json()["id"]
    chronic = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient, "disease": "hypertension", "managed_by_org_id": org})
    assert chronic.status_code == 201, chronic.text
    source = client.post(f"{B}/data-sources", headers=admin, json={
        "code": "p294_ph", "name": "P294 公卫随访", "source_type": "publichealth", "freq_minutes": 60})
    assert source.status_code == 201, source.text
    return {"chronic": chronic.json()["id"], "source": source.json()["id"]}


def _followups(chronic_id, n):
    """一口气录 n 条只有收缩压的公卫随访（集中录入日），返回随访号。"""
    from app.database import SessionLocal
    from app.models import FollowUp

    with SessionLocal() as db:
        rows = [FollowUp(chronic_id=chronic_id, sbp=120 + i % 40) for i in range(n)]
        db.add_all(rows)
        db.commit()
        return [r.id for r in rows]


def _synced(followup_ids):
    from app.database import SessionLocal
    from app.spd.models import SpdMeasurement

    refs = [f"chronic_fu:{i}:bp_sys" for i in followup_ids]
    with SessionLocal() as db:
        return db.query(SpdMeasurement).filter(SpdMeasurement.source_ref.in_(refs)).count()


def _run(source_id):
    from app.database import SessionLocal
    from app.spd.collectors import run_source
    from app.spd.models import SpdDataSource

    with SessionLocal() as db:
        log = run_source(db, db.get(SpdDataSource, source_id))
        db.commit()
        return log.success, log.rows


def test_窗口里多于500条随访_一轮全部同步_不再卡在最早的500条(world):
    ids = _followups(world["chronic"], 501)
    success, _ = _run(world["source"])
    assert success
    assert _synced(ids) == 501   # 修前 ≤ 500：最后一条（及窗口里排在它前面的别人的随访挤掉的）没进来
    # 再跑一轮不重复落数（来源键判重照旧）
    assert _run(world["source"]) == (True, 0)
    assert _synced(ids) == 501
