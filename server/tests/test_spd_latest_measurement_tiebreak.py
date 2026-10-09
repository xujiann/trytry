"""慢专病「最近一次指标」同一测定时刻按录入先后取后录的那条（P2-707，第十七批「最近 / 最新 / 上次」扫描 U4-4）。

规则事实（`build_facts` 取每项指标的最近一次值，喂给入组 / 排除 / 转诊规则）、居民端首页的「最近指标」、患者档案的最近
50 条监测，三处都只按 `measured_at` 倒序取，没有尾键。`measured_at` 不是唯一列——批量导入与设备上传会造出同一时刻的
多条（同子系统的监测清单早就写着「翻页需要尾键」，`care.py` / `portal.py` 的清单都按 `measured_at, id` 倒序）：同一时刻
两条读数时取哪条由库的返回次序决定，PG 换个执行计划就换一条——监测清单排第一的是后录的那条，规则事实与首页用的
可以是另一条。修法与兄弟清单同一个次序：`measured_at` 倒序、再按 `id` 倒序。

SQLite 上走 (patient_id, metric, measured_at) 索引倒序扫描、并列时恰好按 rowid 倒序，行为上测不出修前修后之分——防回退
靠静态钉（全仓库按 `SpdMeasurement.measured_at` 倒序的 order_by 都要以 `SpdMeasurement.id.desc()` 收尾），行为用例记下约定
（与 P2-693 同一做法）。测定时刻可以写未来的问题并入 P2-443。
"""
import ast
from datetime import datetime
from pathlib import Path

from app.database import SessionLocal

APP = Path(__file__).resolve().parents[1] / "app"


def _measured_desc_orderings() -> list[tuple[str, int, str]]:
    """全仓库里参数含 `SpdMeasurement.measured_at.desc()` 的 order_by 调用：(文件, 行号, 源码)。"""
    found = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "order_by"
                    and any(ast.unparse(a) == "SpdMeasurement.measured_at.desc()" for a in node.args)):
                found.append((str(path.relative_to(APP)), node.lineno, ast.unparse(node)))
    return found


def test_按测定时刻倒序的都以编号倒序收尾():
    sites = _measured_desc_orderings()
    assert len(sites) >= 5, sites   # build_facts / 居民端首页 / 档案 / 两个监测清单
    bad = [s for s in sites if not s[2].endswith("SpdMeasurement.measured_at.desc(), SpdMeasurement.id.desc())")]
    assert not bad, bad   # 修前 3 处：spd/service.py、spd/routers/portal.py、spd/routers/population.py


def _measured_asc_orderings() -> list[tuple[str, int, str]]:
    """升序的那一形（P2-1603，第四十七批扫描 AK2-12）：参数含裸 `SpdMeasurement.measured_at` 的 order_by。

    趋势按测定时刻升序取全部、`latest` 取末行（`rows[-1]`）——与倒序取首行是同一件事，上面只认 `.desc()` 的钉看不到。"""
    found = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "order_by"
                    and any(ast.unparse(a) == "SpdMeasurement.measured_at" for a in node.args)):
                found.append((str(path.relative_to(APP)), node.lineno, ast.unparse(node)))
    return found


def test_按测定时刻升序的也以编号收尾():
    sites = _measured_asc_orderings()
    assert sites, sites   # 至少有趋势这一处（spd/routers/care.py::measurement_trend）
    bad = [s for s in sites if not s[2].endswith("SpdMeasurement.measured_at, SpdMeasurement.id)")]
    assert not bad, bad   # 修前 1 处：spd/routers/care.py 的趋势


def test_同一时刻两条读数_规则事实取后录的那条(client, admin):
    """行为上记下约定的次序（SQLite 上修前修后都绿，见模块文档）。"""
    from app.spd.models import SpdMeasurement
    from app.spd.service import build_facts

    patient_id = client.post("/api/patients", headers=admin, json={
        "name": "P2707 患者", "id_card": "330102197001019990", "gender": "男", "birth_date": "1970-01-01"}).json()["id"]
    at = datetime(2026, 9, 20, 8, 0, 0)
    with SessionLocal() as db:
        db.add(SpdMeasurement(patient_id=patient_id, metric="bp_sys", value=168, unit="mmHg", measured_at=at))
        db.flush()
        db.add(SpdMeasurement(patient_id=patient_id, metric="bp_sys", value=128, unit="mmHg", measured_at=at))
        db.commit()
        assert build_facts(db, patient_id)["bp_sys"] == 128
