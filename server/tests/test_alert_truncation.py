"""两个**预警**端点：预警的输入被截断，等于「没预警」与「真没问题」分不开。

这是同一个病的第七、第八例（前六例：住院账单合计、药品临期预警、质控汇总、
spd 评估统计、spd 宣教统计、疫苗临期预警 + 召回追溯总人次）。到这一步已经能
把形状说死：**凡是「先 `.limit(N)` 取行、再拿这批行算出给人看的结论」的，
那个结论都是错的**，而且错得不报警。

| 端点 | 原上限 | 症状 |
|---|---|---|
| `drgs:in_stay_alerts` | 500 | 第 501 张在院床位的超期病例不会被预警 |
| `spd:referral_alerts` | 200 | 积压 500 单时报「超时 200 单」 |

两处修法不同，因为**截断砍掉的那一端不一样**：

- `in_stay_alerts` 按 `id DESC` 排，砍掉的是**较早入院**的病例——而住院越久越该预警，
  砍错了端。行数由**在院床位数**封顶（`status == "admitted"`），不随历史增长，
  所以直接去掉上限；同函数里的历史基线查询本来也没有上限，两边口径就此一致。
- `referral_alerts` 按 `created_at` **升序**排，留下的正是最久未推进的那些——
  **砍的是对的那一端**，所以列表保留 200 条上限，只把 `count` 改成 SQL 总数。
  这两者的对比正好说明：同样是「取完再截断」，**升序还是降序决定了它砍掉的是
  最该看的还是最不该看的**，不能一律照搬同一种修法。
"""
import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import Admission, Bed, Organization, Patient, Ward, utcnow
from app.spd.models import SpdReferralCase


@pytest.fixture(scope="module")
def seeded(client, admin):
    """520 张在院病例（超过 500 上限）+ 260 单超时在途转诊（超过 200 上限）。"""
    from datetime import timedelta

    org = client.post("/api/organizations", headers=admin,
                      json={"name": "预警统计院", "org_type": "lead_hospital",
                            "level": "county"}).json()
    with SessionLocal() as db:
        # admissions.patient_id 有唯一约束（一名患者同时只能有一条在院记录），
        # 所以 520 张在院床位要 520 个不同的患者
        db.execute(insert(Patient), [
            {"ehc_no": f"EHC-AL-{i:04d}", "name": f"预警患者{i}",
             "id_card": f"3301991990{i:08d}", "gender": "male",
             "birth_date": "1990-05-05", "phone": f"139{i:08d}"}
            for i in range(520)
        ])
        db.flush()
        pids = [r[0] for r in db.query(Patient.id)
                .filter(Patient.ehc_no.like("EHC-AL-%")).order_by(Patient.id).all()]
        assert len(pids) == 520
        ward = Ward(org_id=org["id"], name="预警病区")
        db.add(ward)
        db.flush()
        bed = Bed(ward_id=ward.id, bed_no="AL-01", status="occupied")
        db.add(bed)
        db.flush()
        long_ago = utcnow() - timedelta(days=400)
        db.execute(insert(Admission), [
            {"patient_id": pid, "org_id": org["id"], "ward_id": ward.id,
             "bed_id": bed.id, "doctor_name": "预警医生", "diagnosis_name": "观察",
             "status": "admitted", "created_by": 1, "admitted_at": long_ago}
            for pid in pids
        ])
        old = utcnow() - timedelta(hours=200)
        db.execute(insert(SpdReferralCase), [
            {"patient_id": pids[0], "program_code": "al_hyp", "direction": "up",
             "status": "pending", "reason": "预警用", "initiator_org_id": org["id"],
             "current_org_id": org["id"], "current_level": "township",
             "created_by": 1, "created_at": old}
            for _ in range(260)
        ])
        db.commit()
    return {"org": org["id"]}


def test_在院预警不再只看前500张床(client, admin, seeded):
    """520 张在院病例，全都住了 400 天。

    原实现按 id DESC 取前 500 条——第 501 条起（较早入院的那些）**永远不被预警**，
    而住院越久越该预警，砍掉的正是最该看的那一端。
    `ungrouped_in_stay` 数的是「尚未填病案首页的在院病例」，同样少算。
    """
    resp = client.get("/api/drgs/in-stay-alerts", headers=admin,
                      params={"org_id": seeded["org"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 这批都没填病案首页，所以全进 ungrouped_in_stay——它必须是 520 而不是 500
    assert body["ungrouped_in_stay"] == 520, (
        f"在院病例被 500 上限截断了（修复前的症状）：{body['ungrouped_in_stay']}"
    )


def test_转诊超时预警的总数不再是被截断的行数(client, admin, seeded):
    """260 单超时在途，列表上限 200。

    原实现 `count: len(rows)` 会报 200——**与真的只有 200 单无法区分**，
    而这正是拿来判断「积压有多严重」的那个数。
    列表本身仍是 200 条：排序按 `created_at` 升序，留下的是最久未推进的那些，
    砍的是对的那一端。
    """
    resp = client.get("/api/spd/referrals-alerts", headers=admin,
                      params={"hours": 48})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 260, f"总数仍是被截断的行数（修复前的症状）：{body['count']}"
    assert len(body["items"]) == 200, "列表上限保持不变（砍的是对的那一端）"
