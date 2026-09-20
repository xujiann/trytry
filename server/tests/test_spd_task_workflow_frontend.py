"""慢专病任务工作流页的界面入口守卫（P1-40，spd.tasks 10 → 0）。

全平台棘轮只回答"这条路径有没有人调用过"。任务流上有几件它看不见、
写错了后果不轻的事：

* **审核退回必须写意见**：后端 `ReviewTaskIn.note` 是可选的，前端不挡，办理人
  收到的就是一句没有理由的"退回"，只能再猜一遍。
* **提交的 evidence 只能递附件 id**：后端那个字段的注释写着"曾是自由字符串——
  那能让 `require_evidence` 被一串乱码糊弄过去"。前端如果把用户输入原样塞进去，
  等于把那个已经修掉的洞从界面上重新打开。
* **路径调整改的是实例不是模板**：个性化调整不该让同模板的其他患者跟着变。
* **批量分派必须带受派人**：`BatchTaskIn.assignee_id` 是可选的（别的 action
  用不上），唯独 assign 少了它就是一次静默的空操作。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SPD_JS = (STATIC / "pages-spd.js").read_text(encoding="utf-8")


def _block() -> str:
    start = SPD_JS.index("async function renderSpdPath()")
    end = SPD_JS.index("async function renderSpdReferral()", start)
    return SPD_JS[start:end]


BLOCK = _block()


@pytest.mark.parametrize(
    "path",
    [
        "/api/spd/tasks/${",
        "/assign",
        "/submit",
        "/review",
        "/escalate",
        "/api/spd/tasks/batch",
        "/api/spd/tasks-export",
        "/api/spd/path-instances/${",
        "/enter-check",
    ],
)
def test_任务流各端点都在本页调到(path):
    assert path in BLOCK, f"本页没有调用 {path}"


def test_审核退回必须写意见():
    assert "退回必须写明意见" in BLOCK, (
        "审核退回没有强制意见——后端 note 是可选的，办理人收到一句没有理由的"
        "「退回」只能再猜一遍"
    )


def test_提交的evidence只递数字id():
    """后端那个字段的注释写着「曾是自由字符串——那能让 require_evidence 被一串
    乱码糊弄过去」。前端把用户输入原样塞进去，等于把那个洞从界面重新打开。"""
    idx = BLOCK.index("/submit`")
    around = BLOCK[max(0, idx - 900):idx]
    assert ".map((x) => Number(x.trim()))" in around and "filter((x) => x > 0)" in around, (
        "evidence 没有转成数字 id 列表就提交"
    )


def test_批量分派必须带受派人():
    assert '"assign" && !f.assignee_id' in BLOCK, (
        "批量分派没有校验 assignee_id——BatchTaskIn 里它是可选的（别的 action 用不上），"
        "唯独 assign 少了它就是一次静默的空操作"
    )


def test_批量操作先要求勾选():
    assert "先勾选要处理的任务" in BLOCK, "批量操作没有校验勾选，空集会打到后端去"


def test_路径调整改的是实例():
    """盯的是 PATCH 打在 path-instances 而不是 path-templates。

    锚点取 `iAdj.dataset.instAdj` 那一处——`instAdj` 的**第一次**出现是变量
    声明 `const iAdj = el2("data-inst-adj")` 附近，URL 在它之前，窗口往后取
    什么也框不到。这是这一轮第二次踩同一个坑（上一次在 population 那条
    「认领按钮」上踩过），所以这次把锚点写死成调用点的字面形状。
    """
    idx = BLOCK.index("iAdj.dataset.instAdj")
    around = BLOCK[max(0, idx - 400):idx + 200]
    assert "/api/spd/path-instances/${" in around and '"PATCH"' in around, (
        "路径调整没有打在实例上——改模板会让同模板的其他患者跟着变"
    )


def test_升级有二次确认():
    assert "升级会置为紧急并交由上级机构督办" in BLOCK, "升级没有说清后果就执行"


def test_导出口径跟随列表筛选():
    """导出与列表用同一组筛选条件，否则导出的和看到的不是一批数据。"""
    idx = BLOCK.index("tasks-export")
    around = BLOCK[max(0, idx - 300):idx]
    assert 'formJson($("#spd-task-filter"))' in around, (
        "导出没有带上列表的筛选条件——导出的和屏幕上看到的会是两批数据"
    )
