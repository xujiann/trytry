"""体征测量值不收 0 / 负数（P1-101）：慢病随访一次 0/0 就把 3 级高危降成 1 级、上转建议消失。

慢病随访的收缩压 / 舒张压 / 空腹血糖原先没有任何界。2026-09-24 开发库实测（修前代码）：高血压档案先录 185/112
（3 级、建议上转），再录一次 0/0 或 -1/-1——201，分级掉到 1 级「控制良好」、上转建议消失，在管名单里这位患者
从此是 1 级；糖尿病录空腹血糖 0 或 -3 同样降成 1 级。分级按「越高越危」比阈值，0 永远判成控制良好——而 0 正是
血压计测量失败时最常回的值。慢专病的监测值同形状：管理目标多数只设上限（收缩压 ≤ 140、糖化 ≤ 7），按目标判级
时 0 判成「正常」，该有的异常提醒就此漏掉。

修法：慢病随访三项 `gt=0`（收缩压 / 舒张压上界与住院体征 `clinical_docs.VitalIn` 同口径）；急诊体征 `ge=0`
（抢救现场心跳骤停、血压测不出，记 0 是真实的）且不越过生理上限、血氧 ≤ 100；儿童随访身长 / 体重 `gt=0`；
慢专病监测按指标目录（`spd.service.MEASURE_FIELDS`）不收非正数、百分数指标不超过 100
（`spd.service.measure_value_problem`，医护录入、批量上传、居民自测三处同一句）。

**闸门**（派生、零基线）：请求模型里名字是体征的字段（收缩压 / 舒张压 / 血糖 / 心率 / 脉搏 / 呼吸 / 血氧 / 体温 /
身长 / 体重）必须带不小于 0 的下界；按设计可为负的写进 `SIGNED_BY_DESIGN`（冷链设备的温度）。
"""
from __future__ import annotations

import ast
import re

import pytest
import test_datestr_single_source as ds

from app.database import SessionLocal
from app.models import ChronicPatient
from app.spd.models import SpdMeasurement

#: 体征词元：字段名里出现即算
_VITAL = re.compile(r"(^|_)(sbp|dbp|glucose|heart_rate|pulse|respiration|spo2|temperature|height_cm|weight_kg)($|_)")

#: 基线：已清零（2026-09-24 实测 9 处 → 0：慢病随访 3、急诊体征 4、儿童随访 2）
BASELINE = 0

#: 名字像体征、按设计可为负（或不是人体测量）的字段 → 理由（只减不增）
SIGNED_BY_DESIGN = {
    "routers/vaccine_supply.py::ColdChainIn.temperature": "冷链设备的温度，冷冻疫苗零下二十度是常态",
}


def _lower_bound(value: ast.AST | None) -> float | None:
    if not isinstance(value, ast.Call) or ast.unparse(value.func).split(".")[-1] != "Field":
        return None
    for kw in value.keywords:
        if kw.arg in ("ge", "gt"):
            try:  # `ge=-5` 是一元负号不是常量，literal_eval 两种都认
                bound = ast.literal_eval(kw.value)
            except ValueError:
                return None  # 引用了常量名之类：认不出就当没有，交给人看
            if isinstance(bound, (int, float)):
                return float(bound)
    return None


def unbounded_vital_fields() -> list[str]:
    models = ds._model_classes()
    found = []
    for name in ds._request_models(models):
        for path, cls in models[name]:
            for stmt in cls.body:
                if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                        and _VITAL.search(stmt.target.id)):
                    continue
                key = f"{path.relative_to(ds.APP_DIR).as_posix()}::{name}.{stmt.target.id}"
                low = _lower_bound(stmt.value)
                if (low is None or low < 0) and key not in SIGNED_BY_DESIGN:
                    found.append(key)
    return sorted(found)


def test_体征入参必须带不小于0的下界():
    bad = unbounded_vital_fields()
    assert len(bad) <= BASELINE, (
        "以下体征入参收得下 0 以下的值（或根本没有下界）：\n  " + "\n  ".join(bad)
        + "\n\n分级 / 判级按「越高越危」比阈值时，0 与负数永远判成正常。补 `Field(gt=0)`（抢救现场可为 0 的用 ge=0），"
        "上界与住院体征 `clinical_docs.VitalIn` 同口径；按设计可为负的写进 SIGNED_BY_DESIGN。"
    )


def test_按设计名单只许变少():
    names = {f"{p.relative_to(ds.APP_DIR).as_posix()}::{n}.{s.target.id}"
             for n, defs in ds._model_classes().items() for p, cls in defs
             for s in cls.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
    stale = sorted(SIGNED_BY_DESIGN.keys() - names)
    assert stale == [], f"这些字段已经不存在了，请从 SIGNED_BY_DESIGN 划掉：{stale}"


def test_判据自证():
    assert _VITAL.search("sbp") and _VITAL.search("weight_kg") and _VITAL.search("heart_rate")
    assert not _VITAL.search("base_weight") and not _VITAL.search("weight")  # 指标权重、DRG 权重不是体征
    tree = ast.parse("x: float | None = Field(default=None, gt=0)\ny: float | None = None\nz: float = Field(ge=-5)")
    lows = [_lower_bound(s.value) for s in tree.body]
    assert lows == [0.0, None, -5.0]


# ---------------------------------------------------------------- 行为回归


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", json={"name": "体征界限卫生院", "org_type": "township", "level": "township"},
                      headers=admin).json()
    patients = [client.post("/api/patients", json={"name": f"体征患者{i}", "id_card": f"33028119850101{i:04d}"},
                            headers=admin).json()["id"] for i in range(3)]
    return {"org": org["id"], "patients": patients}


def test_慢病随访录0或负数_422_高危分级不被降掉(client, admin, world):
    chronic = client.post("/api/chronic", json={"patient_id": world["patients"][0], "disease": "hypertension",
                                                "managed_by_org_id": world["org"]}, headers=admin).json()
    first = client.post(f"/api/chronic/{chronic['id']}/followups", json={"sbp": 185, "dbp": 112}, headers=admin)
    assert first.status_code == 201 and first.json()["level"] == 3, first.text
    for body in ({"sbp": 0, "dbp": 0}, {"sbp": -1, "dbp": -1}, {"sbp": 3000, "dbp": 90}, {"glucose": 0}):
        resp = client.post(f"/api/chronic/{chronic['id']}/followups", json=body, headers=admin)
        assert resp.status_code == 422, (body, resp.text)
    db = SessionLocal()
    try:
        assert db.get(ChronicPatient, chronic["id"]).level == 3  # 修前：降成 1 级「控制良好」
    finally:
        db.close()
    ok = client.post(f"/api/chronic/{chronic['id']}/followups", json={"sbp": 128, "dbp": 80}, headers=admin)
    assert ok.status_code == 201 and ok.json()["level"] == 1, ok.text


def test_急诊体征0照收_负数与越界422(client, admin):
    # 请求体校验先于业务查找：病例号填不存在的，422 一定来自体征字段
    url = "/api/emergency/cases/999999/vitals"
    for body in ({"heart_rate": -1}, {"sbp": -5}, {"spo2": 101}, {"dbp": 250}):
        assert client.post(url, json=body, headers=admin).status_code == 422, body
    assert client.post(url, json={"heart_rate": 0, "sbp": 0}, headers=admin).status_code == 404  # 心跳骤停记 0 是真实的


def test_儿童随访身长体重0_422(client, admin):
    url = "/api/maternal/children/999999/visits"
    for body in ({"height_cm": 0}, {"weight_kg": -3.2}):
        assert client.post(url, json={"visit_type": "checkup", **body}, headers=admin).status_code == 422, body


def test_慢专病监测值_指标目录里的不收非正数_百分数不超过100(client, admin, world):
    pid = world["patients"][1]
    base = {"patient_id": pid, "program_code": "hypertension"}
    for metric, value in (("bp_sys", 0), ("bp_sys", -1), ("glucose_fasting", 0), ("spo2", 101), ("hba1c", 120)):
        resp = client.post("/api/spd/measurements", json={**base, "metric": metric, "value": value}, headers=admin)
        assert resp.status_code == 422, (metric, value, resp.text)
    # 批量上传里一条不对就整批 422，一条也不落
    batch = client.post("/api/spd/measurements/batch", headers=admin, json={"items": [
        {**base, "metric": "bp_sys", "value": 130}, {**base, "metric": "bp_dia", "value": 0}]})
    assert batch.status_code == 422, batch.text
    db = SessionLocal()
    try:
        assert db.query(SpdMeasurement).filter(SpdMeasurement.patient_id == pid).count() == 0
    finally:
        db.close()
    # 目录外的指标不管（口径由配置决定）；目录里的正常值照收
    assert client.post("/api/spd/measurements", json={**base, "metric": "steps", "value": 0},
                       headers=admin).status_code == 201
    assert client.post("/api/spd/measurements", json={**base, "metric": "bp_sys", "value": 132},
                       headers=admin).status_code == 201


def test_写监测值的处理函数都过同一句判断():
    """慢专病路由里构造 `SpdMeasurement(` 的函数都得调 `measure_value_problem`（医护录入、批量、居民自测）。"""
    missing = []
    for base in ds._ROUTE_DIRS:
        for path in sorted(base.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                calls = {ast.unparse(c.func).split(".")[-1] for c in ast.walk(fn) if isinstance(c, ast.Call)}
                if "SpdMeasurement" in calls and "measure_value_problem" not in calls:
                    missing.append(f"{path.relative_to(ds.APP_DIR).as_posix()}:{fn.name}")
    assert missing == [], missing
