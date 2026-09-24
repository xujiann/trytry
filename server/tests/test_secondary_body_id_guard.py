"""写侧：主对象判了归属，请求体里捎带的另一张机构归属表的 id 却不看是谁家的（P1-80）。

写侧的几道棘轮问的都是「主对象」——路径里的 id（`test_stage15_horizontal.py`）、请求体里的 id
（`test_body_id_org_write_guard.py`）、请求体声明的机构（`test_body_declared_org_write_guard.py`）。
可一个写端点常常还收**第二个**对象：接种时的疫苗批次、交接时的转运人员、排班时的手术间……
主对象判过归属，第二个对象取出来就用，谁家的都行。2026-09-24 实测撞上的是接种登记：
乙院医生登记接种、批次填甲院的，201，**甲院那批的已用数加一**，这一针挂到甲院的批号上（P0-42，已修）。

判据：写端点里 `变量 = db.get(机构归属表, body.x)`（`item.x` / `payload.x` 同），取出后这个变量
既没进 `assert_obj_org_writable`、也没被比对过机构（`变量.*org_id`）或患者（`变量.patient_id`，
与主对象同一患者的一致性校验）。量出 9 条：修 1（接种批次），另有 4 条本就做了同患者一致性校验
（判据收紧后不再计入：耗材记到手术上、续方关联处方、AEFI 关联接种记录、外出就诊关联转诊），
余 4 条逐条判过，分两张名单，只减不增。之后 P1-103 / P1-106 把两处原先一眼不看的团队编号
（分发目标患者、新建任务）改成先取出来查启用，判据才看见它们——行为没变（团队按设计跨机构），
各补登一条按设计，理由写在条目里。
"""
from __future__ import annotations

import ast
import functools
import pathlib
import re

BY_DESIGN = {
    "medwaste.py:handover":
        "交接人（转运员工号与姓名）只是留痕字段，不动库存、不改归属；医废转运常由县域集中转运队伍承担，"
        "经手人不限本机构职工。主对象（这包医废）已按机构判归属。",
    "spd/tasks.py:start_path_instance":
        "路径模板是全县共用的诊疗规范配置（读侧按设计全县可见，见 `test_byid_org_read_guard.BY_DESIGN`），"
        "按别家机构建的模板启动路径是复用规范，不是写别家的数据；主对象（纳管档案）已按机构判归属。",
    "spd/population.py:distribute_candidates":
        "服务团队按设计跨机构：医共体里县级专家团队下沉服务乡镇的目标患者，把目标患者分给别家机构的团队正是分发的用途；"
        "团队停用已拦（P1-103）。主对象（目标池记录）来源与去向两处 assert_org_writable 已判归属。"
        "（P1-103 把取团队的写法从「只查存在」改成取出变量，本判据才看得见它。）",
    "spd/tasks.py:create_task":
        "服务团队按设计跨机构（同上：县级团队下沉服务乡镇），任务挂到别家机构的团队名下正是派活的用途；"
        "团队不存在 / 已停用已拦（P1-106 连带）。主对象（任务归属机构）已 assert_org_writable。"
        "（P1-106 把团队从「交给 spawn_task 原样写库」改成先取出来看启用标志，本判据才看得见它。）",
    "surgery.py:schedule_surgery":
        "手术间撮合按设计跨机构：`resources.match_operating_rooms` 的 docstring 写明「基层把手术病人转上来，"
        "要先看得到县医院哪天有空台」，排进别家的空台正是撮合的下一步；冲突判定与撮合共用一套。"
        "主对象（手术申请）已按机构判归属。",
}

AWAITING = {
    "pathology.py:submit_specimen":
        "病理整个路由没有可见性判定（连调用方都不收），「谁算病理中心、谁能碰别家送的标本」写在待裁定清单"
        "（P1-69 补充的病理一节与 P1-71 问题 2）——答了一起收。",
}


@functools.lru_cache(maxsize=1)
def _org_owned_models() -> frozenset[str]:
    from app import models

    return frozenset(m.class_.__name__ for m in models.Base.registry.mappers
                     if any(c.name.endswith("org_id") for c in m.class_.__table__.columns))


def _unchecked(sources: dict[str, str] | None = None) -> set[str]:
    import test_stage15_horizontal as H

    sources = sources or {}
    org_models = _org_owned_models()
    out: set[str] = set()
    for name, path in H._router_files():
        if name in ("portal.py", "spd/portal.py"):
            continue
        tree = ast.parse(sources.get(name) or pathlib.Path(path).read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.decorator_list]:
            decs = [ast.unparse(d) for d in fn.decorator_list]
            if not any(re.search(r"\.(post|put|patch|delete)\(", d) for d in decs):
                continue
            body = H._with_local_helpers(tree, fn)
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                        and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
                    continue
                call = node.value
                if ast.unparse(call.func) != "db.get" or len(call.args) != 2:
                    continue
                model, arg = ast.unparse(call.args[0]), ast.unparse(call.args[1])
                if model not in org_models or not arg.startswith(("body.", "item.", "payload.")):
                    continue
                var = node.targets[0].id
                checked = (re.search(rf"assert_obj_org_writable\(db, user, {var}\b", body)
                           or re.search(rf"\b{var}\.\w*org_id\b", body)
                           or re.search(rf"\b{var}\.patient_id\b", body))
                if not checked:
                    out.add(f"{name}:{fn.name}")
    return out


def test_覆盖面自证():
    unchecked = _unchecked()
    print(f"\n[写端点捎带的机构归属对象未判] {len(unchecked)} 个（按设计 {len(BY_DESIGN)}、待裁定 {len(AWAITING)}）")
    assert "Voucher" in _org_owned_models() and "VaccineBatch" in _org_owned_models()


def test_不得新增捎带别家对象而不判的写端点():
    assert not (set(BY_DESIGN) & set(AWAITING)), "同一条只能登记在一张名单里"
    new = sorted(_unchecked() - set(BY_DESIGN) - set(AWAITING))
    assert new == [], (
        "以下写端点从请求体取了另一张机构归属表的对象，取出后既不判归属、也不比对机构或患者：\n  "
        + "\n  ".join(new)
        + "\n\n对上主对象的机构（不一致 422，照医废点位 / 接种批次的口径）或 assert_obj_org_writable；"
        "跨机构就是用途的，写明理由登记进 BY_DESIGN。"
    )


def test_名单只许变少():
    stale = sorted((set(BY_DESIGN) | set(AWAITING)) - _unchecked())
    assert stale == [], "这些已补上判定（或已不存在），请从名单里划掉：\n  " + "\n  ".join(stale)


def test_判据自证_拿掉接种批次的归属判定当场点名():
    import test_stage15_horizontal as H

    path = dict(H._router_files())["vaccination.py"]
    text = pathlib.Path(path).read_text(encoding="utf-8")
    fixed = "        if batch.org_id != body.org_id:\n"
    assert fixed in text, "接种登记里找不到 P0-42 的批次归属判定，自证前提变了"
    reverted = text.replace(fixed, "        if False:\n", 1)
    assert "vaccination.py:vaccinate" in _unchecked({"vaccination.py": reverted})
    assert "vaccination.py:vaccinate" not in _unchecked()
