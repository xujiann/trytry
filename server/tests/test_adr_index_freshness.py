"""`docs/adr/README.md` 必须列全 `docs/adr/` 下的每一份 ADR。

## 防的是哪一种失效

2026-09-11 写 ADR-0022 时发现：索引停在 0018，而 0019 / 0020 / 0021 **已经在盘上躺着**
——三份都是 CLAUDE.md §8 要求「请人复核」的越权类决策，却不在索引里。
按索引找"有哪些决策待裁"的人，会以为只有到 0018 为止。

这是同一天第五次撞见「写下来就没人回头核」：`batch_tasks` 的注释、`clock.py` 的
docstring、欠账清单的结论、ROADMAP 的过期数字，现在轮到 ADR 索引。

处理方式与前几次一致：**把"靠人记得"换成"不一致就红"**。
"""
import pathlib
import re

ADR_DIR = pathlib.Path(__file__).resolve().parents[2] / "docs" / "adr"
INDEX = ADR_DIR / "README.md"
#: 模板与索引本身不是 ADR。
NOT_AN_ADR = {"0000-template.md", "README.md"}


def _adr_files() -> list[str]:
    return sorted(
        p.name for p in ADR_DIR.glob("*.md")
        if p.name not in NOT_AN_ADR and re.match(r"^\d{4}-", p.name)
    )


def test_每一份_adr_都在索引里():
    index = INDEX.read_text(encoding="utf-8")
    missing = [name for name in _adr_files() if name not in index]
    assert missing == [], (
        "这些 ADR 在盘上却不在 docs/adr/README.md 的索引里：\n  " + "\n  ".join(missing)
    )


def test_索引里没有已删除的_adr():
    """反方向：索引指向的文件必须还在，否则那行是个死链。"""
    index = INDEX.read_text(encoding="utf-8")
    on_disk = set(_adr_files())
    linked = set(re.findall(r"\((\d{4}-[^)]+\.md)\)", index))
    dangling = sorted(linked - on_disk)
    assert dangling == [], f"索引里这些链接已经指不到文件：{dangling}"


def test_自证_扫描面不为空():
    """防空转：一份 ADR 都数不到时，上面两条恒为真。"""
    assert len(_adr_files()) >= 20, f"只数到 {len(_adr_files())} 份 ADR，扫描面可能不对"
