"""`docs/模块完成度.md` 必须与重新渲染的结果逐字节一致。

与 `test_gate_status_freshness.py` / `test_schema_snapshot_freshness.py` 同一个形状：
一份**靠人记得重跑**的文件，改成"不一致就红"。这份表是「完善各模块功能」的工作清单，
清单过期的后果是按它挑活会挑到已经做完的、漏掉新欠下的——2026-09-11 那天 ROADMAP
上一个过期的"portal 余 48"就是这么误导人的。

它保证「文档 == 渲染」；「渲染 == 代码现状」由 `test_orphan_endpoints.py`（入口列现算，
同一函数）与各闸门自己的用例（其余列只读名单）保证。
"""
import pathlib
import sys

SERVER = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "scripts"))

from dump_module_completeness import OUT, render, rows  # noqa: E402


def test_模块完成度文档与渲染逐字节一致():
    assert OUT.exists(), f"{OUT} 不存在，请运行 python scripts/dump_module_completeness.py"
    assert OUT.read_text(encoding="utf-8") == render(), (
        f"{OUT.name} 与代码/闸门名单不一致——改了端点、前端调用或闸门名单就要重跑：\n"
        "  cd server && python scripts/dump_module_completeness.py"
    )


def test_自证覆盖面_每个路由模块都在表里():
    """防空转：渲染一张空表也会"逐字节一致"。行数必须等于契约闸门枚举到的模块数，
    且 spd 子包（最容易被漏扫的那块）必须在。"""
    import test_orphan_endpoints as orphan

    modules = set(orphan.endpoint_paths().values())
    listed = {r["module"] for r in rows()}
    assert listed == modules, f"表里的模块与枚举不一致：多 {listed - modules} / 少 {modules - listed}"
    assert "spd/config" in listed and len(listed) >= 80


def test_渲染是确定性的():
    assert render() == render()
