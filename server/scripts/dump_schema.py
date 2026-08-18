"""从 SQLAlchemy 元数据生成权威 schema 快照 → docs/schema/SCHEMA.md。

    python scripts/dump_schema.py

这是"输出现有 Schema"（数据模型治理第①步）的可再生实现：数据库结构的唯一真源
是 `app/models.py` + `app/spd/models.py` 的 ORM 定义，本脚本把它导出成人可读的
Markdown，供评审、对接方与治理用例引用。**改了模型就重跑本脚本**，让文档不漂移。

只读，不碰数据库。
"""
from __future__ import annotations

import pathlib
import sys

# 允许从 server/ 直接运行
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.main import app  # noqa: E402,F401  触发全部模型 import（含 spd）
from app.database import Base  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parents[2] / "docs" / "schema" / "SCHEMA.md"


def _col_line(col) -> str:
    parts = [f"`{col.name}`", str(col.type)]
    if col.primary_key:
        parts.append("PK")
    if not col.nullable:
        parts.append("NOT NULL")
    if col.index:
        parts.append("index")
    fks = ", ".join(sorted(fk.target_fullname for fk in col.foreign_keys))
    if fks:
        parts.append(f"→ {fks}")
    return " · ".join(parts)


def main() -> None:
    md = Base.metadata
    tables = sorted(md.tables.values(), key=lambda t: t.name)
    lines: list[str] = [
        "# SCHEMA（自动生成，勿手改）",
        "",
        "> 由 `server/scripts/dump_schema.py` 从 ORM 元数据生成。改了模型请重跑该脚本。",
        f"> 表总数：**{len(tables)}**。类型/关系/迁移的解读见 `docs/DATA_MODEL.md`。",
        "",
    ]
    for t in tables:
        lines.append(f"## {t.name}")
        lines.append("")
        for col in t.columns:
            lines.append(f"- {_col_line(col)}")
        # constraints 是集合，迭代顺序不确定——按列名+名称确定性排序，避免重生成时无谓 churn。
        uniques = sorted(
            (c for c in t.constraints if c.__class__.__name__ == "UniqueConstraint" and c.columns),
            key=lambda u: (tuple(col.name for col in u.columns), u.name or ""),
        )
        for u in uniques:
            cols = ", ".join(c.name for c in u.columns)
            lines.append(f"- _unique_ ({cols}){' ' + u.name if u.name else ''}")
        for idx in sorted(t.indexes, key=lambda i: i.name or ""):
            cols = ", ".join(c.name for c in idx.columns)
            lines.append(f"- _index_ {idx.name}({cols}){' UNIQUE' if idx.unique else ''}")
        lines.append("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写出 {OUT}（{len(tables)} 张表）")


if __name__ == "__main__":
    main()
