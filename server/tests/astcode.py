"""静态闸门的公共前处理：把函数源码里的 **docstring 剥掉再匹配**。

## 为什么要有这个模块

本仓库的静态闸门大多是「在函数源码里找某个名字」——找 `IntegrityError` 判有没有
处理并发冲突、找 `assert_patient_visible` 判有没有做归属校验、找 `paginate(`
判有没有切分页。而 `ast.unparse(fn)` / `ast.dump(fn)` **包含 docstring**，
于是一句散文就能冒充守卫：

    def add_blacklist(...):
        \"\"\"并发下同一患者重复拉黑会撞唯一约束抛 IntegrityError，留待后续处理。\"\"\"
        ...
        db.add(entry)
        db.commit()          # ← 真的没处理，闸门却判定"有守卫"

这句话恰恰是**知道有竞态但还没处理的人**最会写的一句。

## 这个坑本仓库踩过三次

1. ADR-0009 的外壳棘轮——块注释被当成代码；
2. 列表分页棘轮——`quality:record_qc_summary` 的 docstring 里为讲清缺陷原样引了
   一句 `.limit(5000)`，扫描当场把散文里的例子当成代码，一个已经改好的端点
   永远留在计数里，成了还不掉的账。修的时候写下了这句话：

   > 这个坑**两个方向都会错**：docstring 里提一句 `paginate(` 会让一个真有缺陷的
   > 端点被静默排除——那是假阴性，**比多算一条严重得多**。

3. 2026-09-10 的变异审计——上面那句预言应验了，而且是在**另外三道**闸门上：
   并发冲突闸门、横向越权闸门（写侧/批量/读侧三处）、加密列检索点闸门。
   带对照组实测：真拿掉 `except IntegrityError` → 红；同样拿掉、但 docstring 里
   提一句 `IntegrityError` → **绿**。归属校验那条同理，而它是 CLAUDE.md §8 的红线。

**教训被写下来了，只是没有外推。** 所以这次不是再各自修一遍，而是抽成一份共享
实现——下一道闸门写出来时，用的就是已经剥好的源码。

## 判据说明

只剥 **docstring**（模块/类/函数体的首个字符串表达式），不动别的字符串常量：
`detail="…"`、SQL 片段、正则里的字面量都可能是判据要看的东西。
剥完给个 `ast.Pass()` 占位，保证空函数体仍是合法 AST。
"""
from __future__ import annotations

import ast
import copy

_DOC_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def strip_docstrings(node: ast.AST) -> ast.AST:
    """返回**深拷贝**后剥掉全部 docstring 的 AST；原节点不动。"""
    clean = copy.deepcopy(node)
    for sub in ast.walk(clean):
        if not isinstance(sub, _DOC_OWNERS):
            continue
        body = getattr(sub, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            sub.body = body[1:] or [ast.Pass()]
    return clean


def code(node: ast.AST) -> str:
    """`ast.unparse`，但先剥 docstring。**匹配守卫名一律用这个，别用裸 unparse。**"""
    return ast.unparse(strip_docstrings(node))


def dump(node: ast.AST) -> str:
    """`ast.dump`，但先剥 docstring。用于按节点形状匹配的场合。"""
    return ast.dump(strip_docstrings(node))
