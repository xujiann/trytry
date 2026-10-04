"""量表多选题里同一选项写两遍按两遍计分（P2-1274，第三十七批「一次请求、一次导入里的重复元素」扫描 AA2-2）。

`spd/rules.py::score_scale` 对多选题的作答逐个元素累加、不去重；管理端「筛查登记 / 量表评估」逐题作答时多选题是
「（多选，逗号分隔）」文本框，`pages-spd.js::spdCollectAnswers` 按 `[,，、]` 切开、也不去重。修前实测：只有「头晕」一个症状
（2 分低危）录成「头晕,头晕」得 4 分中危、判疑似，三个重复得 6 分高危、进目标池；评估录「胸闷,心悸,胸闷」（应 3 分中危）
得 5 分高危，档案风险改写 high、自动派出一条高危复诊。同函数 docstring 写的是「multi（多选，累加）」，多选题每个选项只能
选一次；居民端自查早已是复选框（P2-363），本就造不出重复。

修法：评分对同一题的多选作答先按标签去重（保持首次出现的顺序）；页面切开后同样去重（仍是文本框，交互形态不动）。
作答里不在选项里的标签照旧按 0 分。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.spd.rules import score_scale

B = "/api/spd"
SRC = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
SYMPTOM = {"key": "sym", "title": "近一周症状", "type": "multi",
           "options": [{"label": "头晕", "score": 2}, {"label": "胸闷", "score": 2},
                       {"label": "心悸", "score": 1}, {"label": "无", "score": 0}]}
SCORING = {"ranges": [{"min": 0, "max": 2, "risk": "low", "advice": "低危"},
                      {"min": 3, "max": 4, "risk": "mid", "advice": "建议复核"},
                      {"min": 5, "max": 100, "risk": "high", "advice": "高危，建议就诊"}]}


def test_同一选项写几遍只计一次():
    once = score_scale([SYMPTOM], {"sym": ["头晕"]}, SCORING)
    assert (once["score"], once["risk_level"]) == (2.0, "low")
    assert score_scale([SYMPTOM], {"sym": ["头晕", "头晕"]}, SCORING) == once            # 修前 4.0 mid
    assert score_scale([SYMPTOM], {"sym": ["头晕", "头晕", "头晕"]}, SCORING) == once    # 修前 6.0 high
    mixed = score_scale([SYMPTOM], {"sym": ["胸闷", "心悸", "胸闷"]}, SCORING)
    assert (mixed["score"], mixed["risk_level"]) == (3.0, "mid")                         # 修前 5.0 high
    # 不同的选项照旧逐项累加；不在选项里的标签照旧按 0 分；单个字符串当一项
    assert score_scale([SYMPTOM], {"sym": ["头晕", "胸闷", "心悸"]}, SCORING)["score"] == 5.0
    assert score_scale([SYMPTOM], {"sym": ["头晕", "乏力", "乏力"]}, SCORING)["score"] == 2.0
    assert score_scale([SYMPTOM], {"sym": "胸闷"}, SCORING)["score"] == 2.0


def test_单选与数值题不受影响():
    items = [{"key": "q1", "type": "single", "options": [{"label": "从不", "score": 0}, {"label": "经常", "score": 3}]},
             {"key": "q2", "type": "number", "score_per_unit": 0.5}, SYMPTOM]
    out = score_scale(items, {"q1": "经常", "q2": 4, "sym": ["心悸", "心悸"]}, SCORING)
    assert (out["score"], out["answered"], out["total_items"]) == (6.0, 3, 3)   # 3 + 0.5×4 + 心悸 1
    assert score_scale(items, {"q1": ["经常", "经常"]}, SCORING)["score"] == 0.0   # 单选交了列表照旧对不上选项、不计分


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21274 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patients = []
    for name, id_card in (("P21274 筛查", "330106197001011274"), ("P21274 评估", "330106197101021274")):
        resp = client.post("/api/patients", headers=admin, json={
            "name": name, "id_card": id_card, "gender": "男", "birth_date": id_card[6:10] + "-01-01"})
        assert resp.status_code in (200, 201), resp.text
        patients.append(resp.json()["id"])
    scale = client.post(f"{B}/scales", headers=admin, json={
        "code": "P21274_SYM", "name": "症状自评（多选）", "category": "risk", "program_code": "hypertension",
        "items": [SYMPTOM], "scoring": SCORING})
    assert scale.status_code == 201, scale.text
    assert client.post(f"{B}/scales/{scale.json()['id']}/publish", headers=admin).status_code == 200
    return {"org": org, "screen": patients[0], "assess": patients[1]}


def test_筛查_同一症状写几遍都不判疑似_不进目标池(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdCandidate

    for answers in (["头晕"], ["头晕", "头晕"], ["头晕", "头晕", "头晕"]):
        resp = client.post(f"{B}/screenings", headers=admin, json={
            "patient_id": world["screen"], "program_code": "hypertension", "org_id": world["org"],
            "scale_code": "P21274_SYM", "answers": {"sym": answers}})
        assert resp.status_code == 201, resp.text
        got = resp.json()
        assert (got["score"], got["risk_level"], got["result"]) == (2.0, "low", "normal"), answers   # 修前 4 中危 / 6 高危疑似
        assert got["answers"] == {"sym": answers}   # 作答照原样记，只是计分不重复
    with SessionLocal() as db:
        assert db.query(SpdCandidate).filter(SpdCandidate.patient_id == world["screen"]).count() == 0   # 修前进池 suspect / high


def test_评估_重复作答不升高危_不派复诊(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment, SpdRevisit

    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": world["assess"], "program_code": "hypertension", "org_id": world["org"], "risk_level": "low"})
    assert enrolled.status_code == 201, enrolled.text
    resp = client.post(f"{B}/assessments", headers=admin, json={
        "patient_id": world["assess"], "scale_code": "P21274_SYM", "program_code": "hypertension",
        "answers": {"sym": ["胸闷", "心悸", "胸闷"]}})
    assert resp.status_code == 201, resp.text
    assert (resp.json()["score"], resp.json()["risk_level"]) == (3, "mid")   # 修前 5 分 high
    with SessionLocal() as db:
        assert db.get(SpdEnrollment, enrolled.json()["id"]).risk_level == "mid"   # 修前回写 high
        assert db.query(SpdRevisit).filter(SpdRevisit.patient_id == world["assess"]).count() == 0   # 修前自动派一条高危复诊


def _function(name: str) -> str:
    start = SRC.index(f"function {name}(")
    return SRC[start:SRC.index("\n}\n", start) + 2]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")
def test_页面切开多选作答时去重_单选数值照旧():
    items = [{"key": "sym", "type": "multi"}, {"key": "q1", "type": "single"}, {"key": "q2", "type": "number"},
             {"key": "q3", "type": "multi"}]
    form = {"q_sym": "头晕,头晕，胸闷、头晕 , 心悸", "q_q1": "经常", "q_q2": "4", "q_q3": "  "}
    script = (_function("spdCollectAnswers")
              + "\nconst [items, form] = JSON.parse(process.argv[1]);"
              + "\nconsole.log(JSON.stringify(spdCollectAnswers(items, form, 'q_')));")
    out = subprocess.run(["node", "-e", script, json.dumps([items, form], ensure_ascii=False)],
                         capture_output=True, text=True, check=True, timeout=60).stdout
    assert json.loads(out) == {"sym": ["头晕", "胸闷", "心悸"], "q1": "经常", "q2": 4}   # 修前 sym 是 头晕×3 + 胸闷 + 心悸
