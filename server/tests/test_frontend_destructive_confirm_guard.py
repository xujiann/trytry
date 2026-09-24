"""点一下就生效的不可逆操作，必须先确认（P2-43）。

P2-38 把弹窗录入逐页换成表单时，接连撞见同一类缺陷——按钮点一下就生效，没有任何确认，
页面上也没有撤回的入口：家医签约「解约」、上门工单「取消」、随访任务「取消」、知识条目「停用」。
按形状全仓库一扫，还有十处：管理端替居民取消预约（号源随即释放）、撤销患者的调阅授权、
互联网诊疗与慢专病咨询「结束」、孕产妇与公卫事件「结案」、「结束派驻」（结束日一律记今天，
而下沉指标按起止日期算）、停用考核公式与规则、删除路径节点。

判据：
- 破坏性调用：`api(` / `authApi(` / `postAction(` 的地址落在 `/cancel` `/close` `/terminate`
  `/end` `/revoke` `/withdraw` `/void` `/scrap` `/cancel-enroll` 上，或方法是 `DELETE`；
- 已确认：同一分支里、调用之前出现过 `confirm(` / `spdModal(` / `cardForm(`。分支按缩进界定：
  单行的 `if (…) …` 只看这一行；块里的调用往上找到块开头为止，`try {` 与多行表达式的续行
  是透明的（确认常写在 `try` 外面、或者 `.then` 前面）。
- 可逆的（再加回去即可）列进 `EXEMPT` 并写理由；新增一个不可逆的破坏性按钮而不确认，即红。
"""
from __future__ import annotations

import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"

CALL = re.compile(r"\b(?:api|authApi|postAction)\(")
DESTRUCTIVE_PATH = re.compile(
    r"/(?:cancel|close|terminate|end|revoke|withdraw|void|scrap|cancel-enroll)(?:[`?/\"$]|$)"
)
GUARDS = ("confirm(", "spdModal(", "cardForm(")
SINGLE_LINE_BRANCH = re.compile(r"^(?:\}\s*)?(?:else\s+)?if\s*\(")

#: 可逆的破坏性操作：按调用地址里的一段稳定字面量豁免，理由写清"怎么恢复"。只减不增。
EXEMPT = {
    "/api/appointments/blacklist/": "移出预约黑名单：同页可再加回去",
    "/api/medwaste/locations/": "暂存点停用：同一按钮再点即重新启用（reactivate）",
    "/api/org-groups/": "移出协作分组：同页可再加回去",
    "/cancel-enroll": "学员本人退报名：可再报名",
    "/api/spd/team-members/": "移出签约团队：同页可再添加",
    "/api/spd/groups/": "移出患者分组：同页可再加入",
}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _code(line: str) -> str:
    """去掉行尾 `//` 注释再判断是否以 `{` 结尾（字符串里的 `//` 不影响：只用于看行尾）。"""
    return line.split("//")[0].rstrip() if "//" in line else line.rstrip()


def _guarded(lines: list[str], i: int) -> bool:
    line = lines[i]
    if any(g in line for g in GUARDS):
        return True
    if SINGLE_LINE_BRANCH.match(line.strip()):
        return False  # 单行分支：作用域就是这一行
    cur = _indent(line)
    for j in range(i - 1, -1, -1):
        prev = lines[j]
        if not prev.strip():
            continue
        if any(g in prev for g in GUARDS):
            return True
        if _indent(prev) < cur:
            stripped = _code(prev).strip()
            if stripped.startswith("try {") or stripped == "{" or not stripped.endswith("{"):
                cur = _indent(prev)  # try 块 / 多行表达式的续行：透明，继续往上
                continue
            return False  # 分支或函数体的开头：出了作用域
    return False


def destructive_calls(root: pathlib.Path = STATIC) -> list[tuple[str, bool]]:
    """(位置: 调用行, 是否已确认)；豁免的不列。"""
    out = []
    for path in sorted(root.rglob("*.js")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not CALL.search(line):
                continue
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            destructive = (
                DESTRUCTIVE_PATH.search(line)
                or '"DELETE"' in line
                or ('"DELETE"' in nxt and not CALL.search(nxt))
            )
            if not destructive or any(key in line for key in EXEMPT):
                continue
            where = f"{path.relative_to(root).as_posix()}:{i + 1}: {line.strip()[:100]}"
            out.append((where, _guarded(lines, i)))
    return out


def test_不可逆操作点下去之前必须先确认():
    unguarded = [where for where, ok in destructive_calls() if not ok]
    assert unguarded == [], (
        "这些破坏性操作点一下就生效、没有确认——先 spdModal / confirm 说清后果，取消即放弃；"
        "若确实可逆（再加回去即可），把地址里的稳定片段加进 EXEMPT 并写明怎么恢复：\n  "
        + "\n  ".join(unguarded)
    )


def test_判据自证(tmp_path):
    """植回原样缺陷必须抓到；三种写法正确的确认不能误报；扫描面确实覆盖到各端。"""
    bad = tmp_path / "bad.js"
    bad.write_text(
        "page.onclick = async (e) => {\n"
        "  if (close) return postAction(`/api/publichealth/events/${close}/close`, null, \"#m\");\n"
        "  if (term) { await api(`/api/contracts/${term}/terminate`, { method: \"POST\" }); route(); }\n"
        "  if (del) {\n"
        "    await api(`/api/spd/path-nodes/${del}`,\n"
        "      { method: \"DELETE\" });\n"
        "  }\n"
        "};\n",
        encoding="utf-8",
    )
    assert [ok for _, ok in destructive_calls(tmp_path)] == [False, False, False]
    bad.unlink()
    good = tmp_path / "good.js"
    good.write_text(
        "page.onclick = async (e) => {\n"
        "  if (close) {\n"
        "    if (!await spdModal(\"结案\", [], { intro: \"不能重开\" })) return;\n"
        "    return postAction(`/api/publichealth/events/${close}/close`, null, \"#m\");\n"
        "  }\n"
        "  if (revoke) {\n"
        "    if (!confirm(\"撤回？\")) return;\n"
        "    try {\n"
        "      await api(`/api/consents/${revoke}/revoke`, { method: \"POST\" });\n"
        "    } catch (err) {}\n"
        "  }\n"
        "  if (cvoid) {\n"
        "    return spdModal(\"作废\", [\n"
        "      { name: \"reason\" },\n"
        "    ]).then((form) =>\n"
        "      form && postAction(`/api/credentials/${cvoid}/void`, form, \"#m\"));\n"
        "  }\n"
        "};\n",
        encoding="utf-8",
    )
    assert [ok for _, ok in destructive_calls(tmp_path)] == [True, True, True]
    found = destructive_calls()
    assert len(found) >= 20, f"破坏性调用只扫到 {len(found)} 处（2026-09-24 约 30 处），判据可能失灵"
    assert any(w.startswith("m/") for w, _ in found), "移动端 app/static/m/*.js 不在扫描面里"
