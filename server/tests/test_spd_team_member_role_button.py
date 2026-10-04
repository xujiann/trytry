"""团队工作台成员表的「改角色」不再被当成视角切换按钮（P2-1332）。

`renderSpdTeam` 的点击处理先按 `[data-role]` 认视角切换按钮（团队专家端 / 团队成员端 / 个案管理师端），而成员表每行的
「改角色」按钮也带着 `data-role="成员角色"`——点「改角色」不弹框，反倒把 doctor 之类的成员角色写进 localStorage 的
`spd_team_role`，工作台回 422「role：格式不对」、整页只剩报错；这个键一直留着，刷新还是坏的，视角按钮也画不出来，
只能清浏览器存储（第三十八批修复子代理用临时端到端探针实测）。成员角色恰好是 expert / case_manager 时更隐蔽：不报错，
只是悄悄切了视角。

修后「改角色」按钮改用 `data-member-role`；存下的视角不是那三种之一的按成员端，已经被改坏的浏览器打开即恢复。
端到端见 `tests/e2e/test_flows.py::test_团队工作台成员改角色弹框_视角不被改坏_坏值按成员端`。
"""
import re
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def _render_team() -> str:
    start = PAGE.index("async function renderSpdTeam()")
    return PAGE[start:PAGE.index("\nasync function ", start + 1)]


def test_页面里带data_role的只有视角切换按钮():
    body = _render_team()
    assert body.count("data-role=") == 1, re.findall(r".{40}data-role=.{30}", body)   # 修前 2：「改角色」按钮也带
    assert 'data-role="${k}"' in body                                               # 剩下的那个是视角切换
    assert 'const roleBtn = el("data-role")' in body                                # 点击处理照旧按它认视角按钮


def test_改角色按钮用data_member_role_弹框按它预选():
    body = _render_team()
    button = re.search(r"<button[^>]*data-tm-edit=[^>]*>改角色</button>", body, re.S)
    assert button, "找不到「改角色」按钮"
    assert 'data-member-role="${esc(m.member_role)}"' in button.group(0)
    assert "data-role" not in button.group(0)
    assert "value: tmEdit.dataset.memberRole," in body
    assert "tmEdit.dataset.role" not in body


def test_存下的视角不认得就按成员端():
    body = _render_team()
    assert 'localStorage.getItem("spd_team_role") || "member"' not in body   # 修前：存了什么用什么
    assert ('const role = stored && Object.prototype.hasOwnProperty.call(roleNames, stored) ? stored : "member";'
            in body)
    # roleNames 要在取工作台之前定下来：视角先校验、再拿去请求
    assert body.index("const roleNames = {") < body.index("api(`/api/spd/workbench/team?role=${role}`)")
