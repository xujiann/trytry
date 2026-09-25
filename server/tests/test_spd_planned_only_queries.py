"""慢专病查询里「只认 planned / running」的写法要登记理由（P1-128 的闸门）。

超期扫描把过了日期的随访记录与复诊从 planned 置为 overdue；路径实例除了执行中还有暂停。只认 planned（或只认 running）
的查询在扫描之后就漏掉超期的（或暂停的）那些——工作台的超期随访恒为 0、到期数只剩今天的、结案不收逾期复诊与暂停的
路径、暂停着的路径能重复启动（案情见 `test_spd_overdue_swept_open.py`）。

**闸门**（派生、零基线）：`app/spd` 里对随访记录 / 复诊的 `status == "planned"`、对路径实例的 `status == "running"`，
以及对这三张表手写的状态清单（`status.in_([...])`），都得登记在 `BY_DESIGN`、写明为什么只认这一个；要的是「没做完」
「没结束」就用 `service.FOLLOWUP_OPEN_STATUSES` / `REVISIT_OPEN_STATUSES` / `PATH_OPEN_STATUSES` 与 `followup_overdue`。
"""
import ast
import pathlib

SPD = pathlib.Path(__file__).resolve().parents[1] / "app" / "spd"
#: 表 → 它的「只认一个」的那个状态
SINGLE = {"SpdFollowupRecord": "planned", "SpdRevisit": "planned", "SpdPathInstance": "running"}

#: `文件:函数:表` → 为什么只认这一个（只减不增）
BY_DESIGN = {
    "service.py:followup_overdue:SpdFollowupRecord": "超期判定里扫描间隙的那一支：还是 planned、日期已过的（另一支是已标超期）",
    "service.py:sweep_overdue:SpdFollowupRecord": "超期扫描本身：只把 planned 置为 overdue",
    "service.py:sweep_overdue:SpdRevisit": "超期扫描本身：只把 planned 置为 overdue",
    "routers/portal.py:home:SpdRevisit": "居民首页的「待复诊」是日期未到的预约（plan_date ≥ 今天）；逾期没来的不再是预约",
    "routers/workbench.py:_path_stats:SpdPathInstance": "按状态分列的计数：执行中与暂停各数各的",
}


def planned_only(sources: dict[str, str] | None = None) -> list[str]:
    """`文件:函数:表`——对这三张表只认一个状态（或手写状态清单）的查询。"""
    files = {p.relative_to(SPD).as_posix(): p.read_text(encoding="utf-8")
             for p in sorted(SPD.rglob("*.py")) if "__pycache__" not in p.parts}
    files.update(sources or {})
    found = []
    for rel, text in files.items():
        tree = ast.parse(text)
        owners = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    owners.setdefault(id(node), fn.name)
        for node in ast.walk(tree):
            table = None
            if (isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
                    and isinstance(node.comparators[0], ast.Constant)):
                left = ast.unparse(node.left)
                for model, status in SINGLE.items():
                    if left == f"{model}.status" and node.comparators[0].value == status:
                        table = model
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("in_", "notin_")
                  and node.args and isinstance(node.args[0], (ast.List, ast.Tuple, ast.Set))):
                left = ast.unparse(node.func.value)
                table = next((m for m in SINGLE if left == f"{m}.status"), None)
            if table:
                found.append(f"{rel}:{owners.get(id(node), '<module>')}:{table}")
    return sorted(found)


def test_只认一个状态的查询都登记了理由():
    found = planned_only()
    unregistered = sorted(set(found) - set(BY_DESIGN))
    assert not unregistered, (
        "以下查询对随访 / 复诊只认 planned、对路径实例只认 running（或手写了状态清单）：\n  " + "\n  ".join(unregistered)
        + "\n\n超期扫描一过，planned 的都成了 overdue；路径还有暂停的。要「没做完 / 没结束」用 "
        "`service.FOLLOWUP_OPEN_STATUSES` / `REVISIT_OPEN_STATUSES` / `PATH_OPEN_STATUSES`，要超期用 "
        "`followup_overdue`；确属只认这一个的，登记进 BY_DESIGN 写明理由（P1-128）。"
    )
    assert set(BY_DESIGN) <= set(found), f"名单里的 {sorted(set(BY_DESIGN) - set(found))} 已不在代码里，划掉"
    assert all(reason.strip() for reason in BY_DESIGN.values())


def test_未完成集合含超期_未结束集合含暂停():
    from app.spd.models import SpdFollowupRecord, SpdPathInstance, SpdRevisit
    from app.spd.service import FOLLOWUP_OPEN_STATUSES, PATH_OPEN_STATUSES, REVISIT_OPEN_STATUSES
    from test_status_text_from_backend import _column_codes

    assert set(FOLLOWUP_OPEN_STATUSES) == {"planned", "overdue"} <= _column_codes(SpdFollowupRecord, "status")
    assert set(REVISIT_OPEN_STATUSES) == {"planned", "overdue"} <= _column_codes(SpdRevisit, "status")
    assert set(PATH_OPEN_STATUSES) == {"running", "paused"} <= _column_codes(SpdPathInstance, "status")


def test_判据自证_只认一个的点名_共享集合与别的状态放过():
    snippet = (
        "def due(db):\n"
        "    a = db.query(SpdFollowupRecord).filter(SpdFollowupRecord.status == 'planned').count()\n"
        "    b = db.query(SpdRevisit).filter(SpdRevisit.status.in_(['planned', 'overdue'])).count()\n"
        "    c = db.query(SpdPathInstance).filter(SpdPathInstance.status == 'running').count()\n"
        "    d = db.query(SpdFollowupRecord).filter(SpdFollowupRecord.status.in_(FOLLOWUP_OPEN_STATUSES)).count()\n"
        "    e = db.query(SpdFollowupRecord).filter(SpdFollowupRecord.status == 'done').count()\n"
        "    f = db.query(SpdTask).filter(SpdTask.status == 'planned').count()\n"
    )
    assert [f for f in planned_only({"probe.py": snippet}) if f.startswith("probe.py")] == [
        "probe.py:due:SpdFollowupRecord", "probe.py:due:SpdPathInstance", "probe.py:due:SpdRevisit"]
