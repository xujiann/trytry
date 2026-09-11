"""`docs/闸门现状.md` 必须与各闸门的登记基线逐字节一致。

## 防的是哪一种失效

与 `test_schema_snapshot_freshness.py` 同一个形状：一份**靠人记得重跑**的文件。

值得单列一条，是因为 2026-09-11 这一天里连撞三次「写下来就没人回头核」：

* `spd/tasks.py:batch_tasks` 的注释说单条接口一直有校验——那个接口恰恰没有；
* `app/clock.py` 的 docstring 说 `test_clock.py` 有一条扫描用例——那个文件当时不存在；
* 欠账清单的注释说 `adjust_path_instance` 修不了——「没有机构列」是真的，
  「所以校验不了」是错的（归属隔一跳外键就能拿到）。

同一天还发现 `ROADMAP.md` 把**已经清零**的契约棘轮仍写成「portal 余 48」——
按路线挑活会挑到一件早就做完的事。数字写进文档那一刻起就开始腐烂，
除非有东西盯着。

## 它**不**保证什么

它比的是「文档 ↔ 闸门里的常量」，**不是**「常量 ↔ 代码现状」。
后者是各闸门自己那条用例的职责（每条都断言实测 == 基线）。
这里刻意不重跑扫描：再写一遍测量逻辑就是第二份实现，两份实现迟早会飘，
那时这条会在真闸门已经坏掉的情况下继续报绿。
"""
import pathlib
import sys

SERVER = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "scripts"))

from dump_gate_status import OUT, _rows, render  # noqa: E402


def test_闸门现状文档与登记基线逐字节一致():
    assert OUT.exists(), f"{OUT} 不存在，请运行 python scripts/dump_gate_status.py"
    assert OUT.read_text(encoding="utf-8") == render(), (
        f"{OUT.name} 与闸门里的登记基线不一致——改了基线就要重跑：\n"
        "  cd server && python scripts/dump_gate_status.py"
    )


def test_自证覆盖面_每个闸门模块都在表里():
    """防空转：渲染出一张空表也会"逐字节一致"。

    这里钉的是**覆盖面**——六个有数字基线的闸门模块必须都出现在表里。
    新立一条带基线的闸门却忘了收进来，就该在这里红。
    """
    sources = {where for *_, where in _rows()}
    expected = {
        "tests/test_stage15_horizontal.py",
        "tests/test_unscopable_patient_reads.py",
        "tests/test_api_contract_governance.py",
        "tests/test_list_pagination_ratchet.py",
        "tests/test_stage14_concurrency.py",
        "tests/test_clock.py",
    }
    missing = sorted(expected - sources)
    assert missing == [], f"这些闸门有数字基线却没进闸门现状表：{missing}"
    assert len(_rows()) >= 15, "表里行数异常，渲染可能在空转"


def test_渲染是确定性的():
    """两次渲染必须一模一样——带时间戳或集合顺序的渲染会让文档天天"漂"，
    最后没人愿意重跑脚本，这份文件就退化成摆设。
    """
    assert render() == render()
