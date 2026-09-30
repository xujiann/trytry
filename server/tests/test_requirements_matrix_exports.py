"""需求对照表把工作量导出、目标池导出、路径导出存档都算在 `GET /api/spd/tasks-export` 头上（P2-944，第二十六批
「导出 / 打印 / 下载 vs 页面」扫描 H1-5）。

《需求对照表》是投标响应与验收核对用的（P2-561）。卫健端 #11「工作量……报表与导出」、专家端 #11 与成员端 #19「工作量……
并导出」、个案管理师端 #4「路径导出与打印存档」的实现栏都写着 `tasks-export`；这个导出只收病种 / 状态 / 机构 / 团队 /
类型 / 只看我的 / 无人认领，不按周期、不按人员汇总，也不按路径实例筛、表头没有节点与办理结果——工作量页按「统计周期 +
医生」出数，导出却回跨两个月的全部任务，挑不出一位患者的一条路径来存档。专家端 #3 写了「……与导出」，实现栏没有任何
导出接口。

修法：对照表如实标成「未交付」（要不要补工作量导出、按路径实例导出随 P2-562 定）。本用例钉住：写着要「导出 / 存档」的
工作量、目标池、路径条目，实现栏要么不提 `tasks-export`，要么写明未交付。
"""
from pathlib import Path

MATRIX = Path(__file__).resolve().parents[2] / "docs" / "全域慢专病全流程管理系统_需求对照表.md"


def _rows():
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].isdigit():
            yield cells[1], cells[2]


def test_工作量_目标池_路径存档的导出不算在任务导出头上():
    checked = 0
    for requirement, implemented in _rows():
        wants_export = ("导出" in requirement and ("工作量" in requirement or "目标池" in requirement)) or (
            "存档" in requirement and "路径" in requirement)
        if not wants_export:
            continue
        checked += 1
        assert "未交付" in implemented, f"「{requirement[:30]}…」的导出没有交付，实现栏却没写明：{implemented}"
    assert checked >= 5   # 卫健端 #11、专家端 #3 / #11、成员端 #19、个案管理师端 #4
