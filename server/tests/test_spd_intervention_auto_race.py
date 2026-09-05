"""高危自动干预/自动复诊的并发防线（P1-30 · spd_interventions 档）。

洞的形状：`care._auto_intervene` 里有两段 check-then-act——在途干预按
(纳管档案, 干预模板) 先查再插、高危复诊按 (患者, 病种) 先查再插——而会话是
`autoflush=False`，调用方提交之前**没有任何语句到库**（连 `enrollment.risk_level`
的回写都不会顺带给档案行上锁）。PG 的 READ COMMITTED 下同一份档案的两次
high/very_high 评估并发，两路都查不到、两路都插：档案上挂两条一模一样的
"高危自动干预"、两条同源高危复诊。SQLite 的库级写锁在这里挡不住（它只锁写，
判定阶段的读根本不排队），但窗口大小全看线程调度——必然的取证在真 PG 档。

修法不是唯一索引，也不是条件 UPDATE：

- `spd_interventions` 的 (enrollment_id, template_id) **不是键**——手工路径
  `create_interventions` 按同一模板批量/按周期反复开具是设计功能，
  `update_intervention` 还允许 removed→planned 恢复，两列本身都可空；
  建唯一索引会把这些合法多行一起拒掉。
- 按 `risk_level` 的条件 UPDATE 同样不行：已经 high 的患者复评仍 high、
  而上一次自动干预已办结/移除时必须重新开一条，条件 UPDATE 会静默掐掉它，
  正是 `_auto_intervene` docstring 里"高危了但系统没动静"那个最坏状态。

于是整段"查 → 判 → 插 → 提交"圈进以档案行为界的临界区
（`concurrency.serialized_on(db, SpdEnrollment, enrollment.id)`）。抢输的一路
重查时读到的是赢家提交后的行，跳过 db.add——与顺序发生的第二次评估完全一样：
不报 409，接口照旧 201，只是不再多写一条。

因此本文件的两层网：

- 行为面：八路并发高危评估全部 201、评估记录八条、档案风险等级回写到位，
  而自动干预与自动高危复诊各恰一条；顺序重复评估同样只有一条（去重没被
  临界区改坏），自动干预办结后再评估**要能再开一条**（去重不是永久闸门）。
- 防拆卸面：上面那条线程探针**红绿取决于调度**——SQLite 的库级写锁只在第一条
  写语句才生效，判定阶段的读根本不排队，所以拆掉锁多半会红，但"多半"不是
  回归测试该给的保证（实测拆成 `nullcontext` 后它确实红了，可那是运气好）。
  确定性的网是静态钉：查/插/提交必须整段在
  `serialized_on(db, SpdEnrollment, enrollment.id)` 块内，且 `db.commit()`
  在块内——提交挪到块外，PG 的 FOR UPDATE 随事务提交释放、SQLite 的进程锁
  也活不过提交，等于没锁。

真 PG 的语义（窗口真实打开、赢家提交后输家才重查）由
`tests/test_spd_intervention_unique_races.py` 守。
"""
import ast
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

CARE_PATH = Path(__file__).resolve().parents[1] / "app" / "spd" / "routers" / "care.py"

#: 综合风险量表（app/spd/seed.py `assess_risk_common`，与病种无关）的满分答法：
#: 4+4+6+4=18 ≥ 14 → very_high，落进 `_auto_intervene` 的触发区间。
VERY_HIGH_ANSWERS = {
    "control": "未达标", "adherence": "差",
    "complication": "2项及以上", "selfcare": "完全依赖",
}


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    from conftest import login

    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def world(client, admin):
    """机构 + 一个 very_high 自动干预模板；患者与档案各用例现造（互不串味）。"""
    org = client.post(
        "/api/organizations",
        json={"name": "自动干预竞态卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    template = client.post(
        "/api/spd/intervention-templates",
        json={"code": "race_auto_vh", "name": "极高危自动干预包",
              "program_code": "hypertension", "category": "drug",
              "content": "药物调整", "measures": "两周内复查血压",
              "frequency": "每周一次", "auto_risk_level": "very_high"},
        headers=admin,
    )
    assert template.status_code == 201, template.text
    return {"org": org, "template": template.json()}


def _enrolled(client, admin, world, name, id_card):
    """现造一位患者 + 一条在管高血压档案，返回 (patient_id, enrollment_id)。"""
    patient = client.post(
        "/api/patients",
        json={"name": name, "id_card": id_card, "gender": "男", "birth_date": "1990-01-01"},
        headers=admin,
    )
    assert patient.status_code in (200, 201), patient.text
    pid = patient.json()["id"]
    enrollment = client.post(
        "/api/spd/enrollments",
        json={"patient_id": pid, "program_code": "hypertension",
              "org_id": world["org"]["id"], "risk_level": "low"},
        headers=admin,
    )
    assert enrollment.status_code == 201, enrollment.text
    return pid, enrollment.json()["id"]


def _assess(client, admin, patient_id):
    return client.post(
        "/api/spd/assessments",
        json={"patient_id": patient_id, "scale_code": "assess_risk_common",
              "program_code": "hypertension", "answers": VERY_HIGH_ANSWERS},
        headers=admin,
    )


def _auto_rows(client, admin, world, patient_id):
    """(自动干预条数, 高危复诊条数)——都只数在途/待办的那些。"""
    interventions = client.get(
        f"/api/spd/interventions?patient_id={patient_id}", headers=admin
    ).json()
    revisits = client.get(
        f"/api/spd/revisits?patient_id={patient_id}", headers=admin
    ).json()
    return (
        [i for i in interventions
         if i["template_id"] == world["template"]["id"] and i["status"] in ("planned", "doing")],
        [r for r in revisits if r["source"] == "high_risk" and r["status"] == "planned"],
    )


# ================================================================ 行为面


def test_同档案八路并发高危评估_自动干预与高危复诊各恰一条(client, admin, world):
    """八路并发极高危评估：八条评估、八个 201，但自动干预与高危复诊各只有一条。

    没有 409：这条路径是幂等而不是互斥，抢输的一路重查看到赢家的行就跳过——
    与顺序发生的第二次评估的行为完全一致。
    """
    patient_id, enrollment_id = _enrolled(
        client, admin, world, "并发评估患者", "330281199001010012"
    )

    barrier = threading.Barrier(8)
    results: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(_i):
        try:
            barrier.wait(timeout=30)
            resp = _assess(client, admin, patient_id)
            with lock:
                results.append(resp.status_code)
        except BaseException as exc:  # noqa: BLE001 - 收集断言用
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"并发评估不该抛错：{errors}"
    assert results == [201] * 8, f"八路都该 201（不是互斥、不该有 409），实际 {results}"

    assessments = client.get(
        f"/api/spd/assessments?patient_id={patient_id}", headers=admin
    ).json()
    assert len(assessments) == 8, f"八次评估一条都不能丢，实际 {len(assessments)}"

    enrollment = client.get(f"/api/spd/enrollments/{enrollment_id}", headers=admin).json()
    assert enrollment["risk_level"] == "very_high", "风险等级回写不能被临界区吞掉"

    autos, revisits = _auto_rows(client, admin, world, patient_id)
    assert len(autos) == 1, f"同一档案同一模板的在途自动干预只该一条，实际 {len(autos)}"
    assert autos[0]["goal"] == "very_high风险自动干预", autos[0]["goal"]
    assert len(revisits) == 1, f"高危自动复诊只该一条，实际 {len(revisits)}"


def test_顺序重复评估仍只开一条自动干预(client, admin, world):
    """临界区不许改变顺序语义：连评三次，自动干预与高危复诊仍各一条、次次 201。"""
    patient_id, _ = _enrolled(client, admin, world, "连评患者", "330281199001010020")
    for _ in range(3):
        assert _assess(client, admin, patient_id).status_code == 201

    autos, revisits = _auto_rows(client, admin, world, patient_id)
    assert len(autos) == 1, f"顺序重复评估只该一条自动干预，实际 {len(autos)}"
    assert len(revisits) == 1, f"顺序重复评估只该一条高危复诊，实际 {len(revisits)}"


def test_自动干预办结后再评估要能再开一条(client, admin, world):
    """去重是"在途去重"，不是永久闸门。

    上一条自动干预办结（不再 planned/doing）之后仍判高危，必须重新开一条——
    这正是"按 risk_level 条件 UPDATE"被否掉的理由：那种写法会把这条静默掐掉。
    """
    patient_id, _ = _enrolled(client, admin, world, "办结再评患者", "330281199001010039")
    assert _assess(client, admin, patient_id).status_code == 201
    autos, revisits = _auto_rows(client, admin, world, patient_id)
    assert len(autos) == 1 and len(revisits) == 1

    done = client.patch(
        f"/api/spd/interventions/{autos[0]['id']}", json={"status": "done"}, headers=admin
    )
    assert done.status_code == 200, done.text
    done_revisit = client.patch(
        f"/api/spd/revisits/{revisits[0]['id']}", json={"status": "done"}, headers=admin
    )
    assert done_revisit.status_code == 200, done_revisit.text

    assert _assess(client, admin, patient_id).status_code == 201
    autos2, revisits2 = _auto_rows(client, admin, world, patient_id)
    assert len(autos2) == 1, "办结后复评必须重新开一条在途自动干预"
    assert autos2[0]["id"] != autos[0]["id"], "开出来的必须是新的一条，不是把旧的翻回来"
    assert len(revisits2) == 1, "办结后复评必须重新排一次高危复诊"


# ================================================================ 防拆卸静态钉


def _auto_intervene_func() -> ast.FunctionDef:
    tree = ast.parse(CARE_PATH.read_text(encoding="utf-8"))
    func = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_auto_intervene"), None
    )
    assert func is not None, "care.py 里找不到 _auto_intervene"
    return func


def _critical_section(func: ast.FunctionDef) -> ast.With:
    block = next(
        (n for n in ast.walk(func)
         if isinstance(n, ast.With) and any(
             ast.unparse(item.context_expr) == "serialized_on(db, SpdEnrollment, enrollment.id)"
             for item in n.items
         )), None
    )
    assert block is not None, (
        "_auto_intervene 里没有 `with serialized_on(db, SpdEnrollment, enrollment.id):`"
        "——两段 check-then-act 又裸着了，PG 上并发评估会写出两条自动干预"
    )
    return block


def _calls(node: ast.AST, matches) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call) and matches(n)]


def _is_ctor(name: str):
    return lambda call: isinstance(call.func, ast.Name) and call.func.id == name


def _is_method(name: str):
    return lambda call: isinstance(call.func, ast.Attribute) and call.func.attr == name


def test_自动干预的查与插必须整段在纳管档案行临界区内():
    """查、判、插三步必须同在一个临界区里——只把 db.add 圈进去等于没圈。

    上面那条线程探针的红绿取决于调度（SQLite 的库级写锁只锁写、不锁判定阶段的读），
    这条静态钉才是确定性的网。
    """
    func = _auto_intervene_func()
    block = _critical_section(func)

    for ctor in ("SpdIntervention", "SpdRevisit"):
        inside = _calls(block, _is_ctor(ctor))
        whole = _calls(func, _is_ctor(ctor))
        assert len(inside) == 1, f"{ctor}(...) 应恰在临界区内构造一次，实际 {len(inside)}"
        assert len(whole) == len(inside), f"临界区外还有 {ctor}(...)，去重就漏了那一条"

    inside_first = _calls(block, _is_method("first"))
    whole_first = _calls(func, _is_method("first"))
    assert len(inside_first) >= 2, (
        f"模板/在途干预/高危复诊的存在性查询应全在临界区内，实际块内只有 {len(inside_first)} 条 .first()"
    )
    assert len(whole_first) == len(inside_first), (
        "临界区外还有 .first() 存在性查询——锁外读到的是旧快照，判定照旧作废"
    )


def test_自动干预必须在临界区内提交():
    """commit 必须在块内且是块内最后一句。

    PG 的 FOR UPDATE 锁随事务提交释放，提交挪到块外 = 没锁；SQLite 侧的进程锁
    也必须活过提交，否则下一位在赢家落盘之前就读完了。
    """
    func = _auto_intervene_func()
    block = _critical_section(func)

    assert len(_calls(block, _is_method("commit"))) == 1, (
        "临界区内应恰有一次 db.commit()——提交挪到块外等于没上锁"
    )
    last = block.body[-1]
    assert isinstance(last, ast.Expr) and ast.unparse(last.value) == "db.commit()", (
        f"db.commit() 必须是临界区里的最后一句，实际最后一句是：{ast.unparse(last)[:80]}"
    )
    assert not _calls(block, _is_method("refresh")), (
        "临界区内不许 db.refresh(enrollment)：档案对象上带着调用方尚未 flush 的 "
        "risk_level 回写，refresh 会把它丢掉（本块没有对档案行的读-改-写，也不需要它）"
    )
