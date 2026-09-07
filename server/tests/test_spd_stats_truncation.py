"""慢专病两个统计端点：先钉住现状，再修「统计吃被截断的样本」。

`GET /api/spd/assessments/stats` 与 `GET /api/spd/edu-pushes/stats` 都是
**聚合统计**，却都先 `.limit(N)` 取行、再在 Python 里算——与刚修掉的
`quality:record_qc_summary` 是同一个病，这已经是第三、第四例：

| 端点 | 上限 | 症状 |
|---|---|---|
| `quality:record_qc_summary`（已修） | 5000 | 6000 份病历报「零丙级」 |
| `spd:assessment_stats` | 5000 | 人数/人次/风险分布/逐题分布全算在前 5000 条上 |
| `spd:edu_stats` | 20000 | 覆盖人数/阅读完成率全算在前 20000 条上 |

`edu_stats` 还多一层：它**连 `order_by` 都没有**——超过 20000 条之后
「取哪 20000 条」由数据库自行决定，于是同一份数据两次请求可能给出不同的完成率。
不是「少算一点」，是**不可复现**。

本文件两段分工（与 `test_qc_summary_*` 同一套做法）：
- `test_特征化_*`：在**当前实现**上写成并跑通，改写后必须一条不改地继续绿 → 钉「没改行为」；
- `test_不再被上限截断_*`：造超过上限的数据 → 钉「修了什么」。
只绿一份的组合都是错的。
"""
import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import Patient
from app.spd.models import SpdAssessment, SpdEduMaterial, SpdEduPush

ASSESS_CAP = 5000   # assessment_stats 原硬编码上限
EDU_CAP = 20000     # edu_stats 原硬编码上限


@pytest.fixture(scope="module")
def seeded(client, admin):
    """两份数据：一份小的照形状，一份超过上限的照截断。

    评估：小样本 4 条覆盖两种风险等级 + 一条未分级（`risk_level=""` 记成"未分级"）；
    然后再灌 `ASSESS_CAP + 100` 条**高风险**——原实现按 id DESC 取前 5000 条，
    小样本那 4 条（id 最小）会被整批挤掉，于是"未分级"凭空消失。

    宣教：小样本 3 条（sms 已读 / sms 已发 / wechat 待发），再灌 `EDU_CAP + 100` 条
    **已读**——原实现取前 20000 条，完成率会被冲成 100%。
    """
    with SessionLocal() as db:
        p1 = Patient(ehc_no="EHC-ST-1", name="统计甲", id_card="330166199001011234",
                     gender="male", birth_date="1990-01-01", phone="13911205001")
        p2 = Patient(ehc_no="EHC-ST-2", name="统计乙", id_card="330166199002021234",
                     gender="female", birth_date="1990-02-02", phone="13911205002")
        db.add_all([p1, p2])
        db.flush()
        mat = SpdEduMaterial(code="ST-MAT-01", program_code="st_hyp",
                             title="统计用材料", media_type="text", content="正文")
        db.add(mat)
        db.flush()

        # ---- 评估：小样本（先插，id 最小，原实现会把它们挤掉）----
        db.add_all([
            SpdAssessment(patient_id=p1.id, scale_id=1, scale_code="ST01",
                          program_code="st_hyp", answers={"q1": "A", "q2": ["X", "Y"]},
                          score=10, risk_level="高"),
            SpdAssessment(patient_id=p2.id, scale_id=1, scale_code="ST01",
                          program_code="st_hyp", answers={"q1": "B"},
                          score=5, risk_level="低"),
            SpdAssessment(patient_id=p2.id, scale_id=1, scale_code="ST01",
                          program_code="st_hyp", answers={"q1": "A"},
                          score=7, risk_level=""),
            SpdAssessment(patient_id=p1.id, scale_id=1, scale_code="ST02",
                          program_code="st_dm", answers={"q9": "Z"},
                          score=3, risk_level="低"),
        ])
        # ---- 宣教：小样本 ----
        db.add_all([
            SpdEduPush(material_id=mat.id, patient_id=p1.id, channel="sms", status="read"),
            SpdEduPush(material_id=mat.id, patient_id=p2.id, channel="sms", status="sent"),
            SpdEduPush(material_id=mat.id, patient_id=p1.id, channel="wechat",
                       status="pending"),
        ])
        db.commit()
        small = {"p1": p1.id, "p2": p2.id, "mat": mat.id}

    return small


@pytest.fixture(scope="module")
def bulk(client, admin, seeded):
    """超过上限的那批，单独一个 fixture——特征化用例必须在灌量**之前**跑。"""
    with SessionLocal() as db:
        p3 = Patient(ehc_no="EHC-ST-3", name="统计丙", id_card="330166199003031234",
                     gender="male", birth_date="1990-03-03", phone="13911205003")
        db.add(p3)
        db.flush()
        db.execute(insert(SpdAssessment), [
            {"patient_id": p3.id, "scale_id": 1, "scale_code": "BULK",
             "program_code": "bulk", "answers": {}, "score": 1, "risk_level": "高",
             "scale_version": "", "advice": "", "channel": "doctor"}
            for _ in range(ASSESS_CAP + 100)
        ])
        db.execute(insert(SpdEduPush), [
            {"material_id": seeded["mat"], "patient_id": p3.id, "channel": "app",
             "status": "read", "send_at": "", "frequency": "once", "fail_reason": ""}
            for _ in range(EDU_CAP + 100)
        ])
        db.commit()
        return {"p3": p3.id}


def _get(client, admin, path, **params):
    resp = client.get(path, headers=admin, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------- 特征化（灌量前）
def test_特征化_评估统计的每个字段(client, admin, seeded):
    s = _get(client, admin, "/api/spd/assessments/stats", scale_code="ST01")
    assert s["persons"] == 2 and s["times"] == 3
    # `risk_level=""` 记成「未分级」——这是现状，重写后必须一样
    assert s["by_risk"] == {"未分级": 1, "低": 1, "高": 1}
    # 逐题分布：列表答案要摊平成多次计数（q2 的 X/Y 各一次）
    assert s["by_item"] == {"q1": {"A": 2, "B": 1}, "q2": {"X": 1, "Y": 1}}


def test_特征化_宣教统计的每个字段(client, admin, seeded):
    s = _get(client, admin, "/api/spd/edu-pushes/stats", program_code="st_hyp")
    assert s["covered_patients"] == 2 and s["push_times"] == 3
    # sent 的口径是「已发 + 已读」，read 只算已读；pending 不进分母
    assert s["sent"] == 2 and s["read"] == 1
    assert s["read_rate"] == 50.0
    assert s["by_channel"] == {"sms": 2, "wechat": 1}


def test_特征化_没有数据时不除零(client, admin, seeded):
    s = _get(client, admin, "/api/spd/edu-pushes/stats", program_code="根本不存在")
    assert s == {"covered_patients": 0, "push_times": 0, "sent": 0, "read": 0,
                 "read_rate": 0.0, "by_channel": {}}


# ---------------------------------------------------------------- 缺陷回归（灌量后）
def test_不再被上限截断_评估统计(client, admin, bulk):
    """`ASSESS_CAP + 100` 条高风险 + 小样本 4 条。

    原实现按 id DESC 取前 5000 条——全是后灌的高风险——于是小样本整批被挤掉，
    「未分级」与「低」凭空消失，人数也少算。
    """
    s = _get(client, admin, "/api/spd/assessments/stats")
    assert s["times"] == ASSESS_CAP + 104, f"人次仍被截断：{s['times']}"
    assert s["persons"] == 3, f"人数仍被截断：{s['persons']}"
    assert s["by_risk"].get("未分级") == 1, (
        f"未分级那条被截断吞掉了（修复前的症状）：{s['by_risk']}"
    )
    assert s["by_risk"].get("低") == 2


def test_不再被上限截断_宣教统计(client, admin, bulk):
    """`EDU_CAP + 100` 条已读 + 小样本 3 条。

    原实现取前 20000 条（且**没有 order_by**，取哪些由数据库决定），
    完成率会被冲成 100%——而且不可复现。
    """
    s = _get(client, admin, "/api/spd/edu-pushes/stats")
    assert s["push_times"] == EDU_CAP + 103, f"次数仍被截断：{s['push_times']}"
    assert s["covered_patients"] == 3
    assert s["by_channel"].get("app") == EDU_CAP + 100
    assert s["by_channel"].get("sms") == 2 and s["by_channel"].get("wechat") == 1
    # 完成率的分母是「已发+已读」，小样本那条 sent 必须还在分母里
    assert s["sent"] == EDU_CAP + 102 and s["read"] == EDU_CAP + 101


# ================================================================ 疫苗侧同形状两处
# 与上面两个统计端点同一个病根：**取回来的那批行不能代表全体**。
# 但这两个端点**没有机构收口**（P1-49），所以只修正确性、不切分页——
# 加 offset/limit 会把可枚举面从「最多 N 行」放大成「整表可翻」，那是另一件事。
from app.models import Organization, VaccinationRecord, VaccineBatch  # noqa: E402


@pytest.fixture(scope="module")
def vaccine(client, admin):
    """520 条早已过期且已发完的批次 + 3 条临期且有余量的；另造 1200 条接种记录。"""
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "疫苗统计院", "org_type": "lead_hospital",
                            "level": "county"}).json()
    with SessionLocal() as db:
        p = Patient(ehc_no="EHC-VAC-1", name="疫苗患者", id_card="330177199001011234",
                    gender="male", birth_date="1990-01-01", phone="13911206001")
        db.add(p)
        db.flush()
        db.execute(insert(VaccineBatch), [
            {"org_id": org["id"], "vaccine_code": "V-OLD", "vaccine_name": "旧苗",
             "batch_no": f"VOLD-{i:04d}", "manufacturer": "厂", "expire_date": "2020-01-01",
             "quantity": 10, "used_quantity": 10, "status": "normal", "frozen_reason": ""}
            for i in range(520)
        ])
        db.execute(insert(VaccineBatch), [
            {"org_id": org["id"], "vaccine_code": "V-SOON", "vaccine_name": "临期苗",
             "batch_no": f"VSOON-{i}", "manufacturer": "厂", "expire_date": "2026-09-20",
             "quantity": 100, "used_quantity": 0, "status": "normal", "frozen_reason": ""}
            for i in range(3)
        ])
        db.flush()
        recall = db.query(VaccineBatch).filter(
            VaccineBatch.batch_no == "VSOON-0").one()
        db.execute(insert(VaccinationRecord), [
            {"patient_id": p.id, "org_id": org["id"], "batch_id": recall.id,
             "vaccine_code": "V-SOON", "vaccine_name": "临期苗", "dose_no": 1,
             "vaccinated_date": "2026-09-01", "created_by": 1}
            for _ in range(1200)
        ])
        db.commit()
        return {"org": org["id"], "recall_batch": recall.id}


def test_疫苗临期预警不再被已发完的过期批次挤掉(client, admin, vaccine):
    """与 pharmacy 那处同形状：升序 + 只有上界 + 取完再筛，砍掉的正是要预警的那端。"""
    resp = client.get("/api/vaccine-supply/expiring", headers=admin,
                      params={"days": 365, "today": "2026-09-07"})
    assert resp.status_code == 200, resp.text
    batches = resp.json()["batches"]
    soon = [b for b in batches if b["batch_no"].startswith("VSOON-")]
    assert len(soon) == 3, f"该预警的批次没出来（修复前的症状）：共 {len(batches)} 条"
    assert not [b for b in batches if b["batch_no"].startswith("VOLD-")]


def test_已过期批次仍要列出来不许被下界筛掉(client, admin, vaccine):
    """修法只下推「仍有余量」，**不许加 expire_date 下界**。

    这个端点的口径是「过期的也一并列出并标注」——不是催人用掉，是提示按报废
    流程处理，别让它躺在冰箱里被误用（见端点 docstring）。加下界等于把这批
    从预警里删掉，是把一个截断缺陷换成一个**口径缺陷**。
    """
    with SessionLocal() as db:
        b = db.query(VaccineBatch).filter(VaccineBatch.batch_no == "VOLD-0000").one()
        b.used_quantity = 0          # 让这条已过期批次「还有余量」
        db.commit()
    resp = client.get("/api/vaccine-supply/expiring", headers=admin,
                      params={"days": 365, "today": "2026-09-07"})
    hit = [b for b in resp.json()["batches"] if b["batch_no"] == "VOLD-0000"]
    assert hit and hit[0]["expired"] is True, "已过期但仍有余量的批次被下界筛掉了"


def test_召回追溯的总人次不再是被截断的行数(client, admin, vaccine):
    """1200 条接种记录、列表上限 1000。

    原实现 `"total": len(rows)` 会报 1000——**与真的只有 1000 人无法区分**，
    而这是召回时用来反查受种者的那个查询，少报意味着少通知。
    """
    resp = client.get(f"/api/vaccine-supply/batches/{vaccine['recall_batch']}/recipients",
                      headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1200, f"总人次仍是被截断的行数：{body['total']}"
    assert len(body["recipients"]) == 1000, "列表本身仍受 1000 行上限（P1-49 未裁定前不动）"
