"""PII 加密列的**检索点**守卫：清单从模型元数据推导，漏改一处当场变红。

## 为什么要有这一条

`app/pii.py` 的开关打开后，`patients.id_card` / `patients.phone` /
`resident_accounts.phone` 落库是密文（每次随机 nonce，同值不同串）。于是：

    db.query(Patient).filter(Patient.id_card == value)      # 恒空

**它不报错**——查不到就是"这人没建过档"，于是重复建档、重复开户、实名绑不上。
正确写法是走 `pii_filter(Patient.id_card_idx, Patient.id_card, value)`（索引列
等值，且在密钥轮换宽限期内并上旧钥口径）。这条纪律此前只写在 `app/pii.py` 的
docstring 与 CLAUDE.md 里——**靠人记得**。本轮的事故正是"漏改一处就查不到"。

第 14 章的做法是把手工清单换成从模型元数据推导（`visibility._relation_tables`）；
第 17 章的做法是让检查工具自证覆盖面。这个文件两条都照做：

* **加密列清单不手写**：扫 `Base.registry`，凡列类型是 `EncryptedPII` 的就是
  加密列，其 `<列名>_idx` 就是它的检索索引列。以后新加一列加密 PII，
  自动进入本守卫的分母，不需要有人记得来改清单。
* **覆盖面打印出来**：扫了几个文件、认出几处检索点、按形态各多少、
  哪几类形态**认不出**（声明的盲区），全部打印，不留"绿了就完事"。

## 判定规则（三条，按后果分级）

1. **明文加密列的等值/包含比较**（``Model.col == x``、``.in_()``、``.is_()``）
   —— 一律违规。等值检索只有 `pii_filter` 一个入口，没有例外。
2. **明文加密列的模糊比较**（``.like()`` / ``.contains()`` …）—— 密文态恒空，
   只允许作为"关态原行为"的一支，且**同一函数里必须出现** `pii_filter` /
   `pii_index_match` 作为开态降级（现状三处都是这个形状：patients.py 的关键字
   检索、spd 的两处证件号筛选）。孤立的模糊比较 = 开态静默失明，违规。
3. **索引列的裸等值**（``Model.col_idx == pii_index(x)``）—— 违规。这正是本轮
   事故的形状：裸写只算当前钥，轮换宽限期内存量行的索引是旧钥算的，一比就漏。
   必须走 `pii_index_match`（`app/pii.py` 自己除外，它是实现处）。

外加一条配对校验：`pii_filter(a, b, …)` 的 a/b 必须是**同一模型同一列**的
索引列与明文列（``X_idx`` 与 ``X``）——写串了同样是静默查不到。

## 认不出的形态（如实声明，见第 17 章第 4 条）

AST 只认"模型类名.列名"这种写法。手写 SQL 字符串、`getattr(Model, 名字)`
动态取列、`filter_by(**动态字典)` 都认不出；本守卫把这三类的实测数量一并
打印出来（一旦有人这么写，数字会先变得可见，而不是悄悄绕过）。当前实测
手写 SQL 0 处、动态取列 0 处、动态 `filter_by` 1 处——那一处是
`concurrency.upsert_unique` 的实现本身，它的**调用侧**由
`_check_keyed_lookup` 按模型解析后逐个检查，不留在盲区里。
"""
from __future__ import annotations

import ast
import pathlib
import warnings

from app.main import app  # noqa: F401  触发全部模型 import（含 spd）
from app.database import Base
from app.pii import EncryptedPII

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1]
#: 扫描范围：应用代码 + 运维脚本。测试自身不扫（用例本就在关态下直接比明文列）。
SCAN_ROOTS = (SERVER_DIR / "app", SERVER_DIR / "scripts")
#: `app/pii.py` 是这套口径的实现处，索引列裸等值在它内部是**定义**而非违规。
IMPLEMENTATION_FILES = {SERVER_DIR / "app" / "pii.py"}

#: 走索引列的两个正确入口。函数里出现其一，模糊比较才算"开态已降级"。
SAFE_HELPERS = ("pii_filter", "pii_index_match")

EQUALITY_METHODS = {"in_", "is_", "isnot", "is_not", "notin_", "not_in"}
FUZZY_METHODS = {
    "like", "ilike", "notlike", "not_like", "notilike",
    "contains", "startswith", "endswith", "icontains", "regexp_match",
}


# --------------------------------------------------------- 清单：从元数据推导

def _mapped_classes() -> dict[str, type]:
    """已映射的 ORM 类：``{类名: 类}``（registry 里还混着 `_ModuleMarker`，跳过）。"""
    return {
        cls.__name__: cls
        for cls in list(Base.registry._class_registry.values())
        if hasattr(cls, "__table__") and hasattr(cls, "__name__")
    }


def encrypted_columns(classes: dict[str, type]) -> dict[str, set[str]]:
    """``{模型类名: {加密列名}}``——列类型是 `EncryptedPII` 就算，不手工枚举。"""
    out: dict[str, set[str]] = {}
    for name, cls in classes.items():
        cols = {c.name for c in cls.__table__.columns if isinstance(c.type, EncryptedPII)}
        if cols:
            out[name] = cols
    return out


def index_columns(classes: dict[str, type], enc: dict[str, set[str]]) -> dict[str, set[str]]:
    """``{模型类名: {检索索引列名}}``——加密列名加 `_idx`，且该列真的存在于表上。

    "真的存在"这一步不能省：`app/pii.py` 的索引口径依赖旁列，模型里没建出来
    就说明有人加了加密列却没加索引列——那种情况由 `test_加密列都有配套的检索索引列`
    单独报出来，而不是在这里被静默跳过。
    """
    out: dict[str, set[str]] = {}
    for name, cols in enc.items():
        present = {c.name for c in classes[name].__table__.columns}
        out[name] = {f"{c}_idx" for c in cols if f"{c}_idx" in present}
    return out


MAPPED = _mapped_classes()
ENCRYPTED = encrypted_columns(MAPPED)
INDEXED = index_columns(MAPPED, ENCRYPTED)


# ------------------------------------------------------------------ AST 扫描

class _Scan:
    """一次全量扫描的结果：违规、已配对豁免、以及"认不出的形态"计数。"""

    def __init__(self) -> None:
        self.files = 0
        self.sites: list[str] = []          # 认出的全部检索点
        self.violations: list[str] = []     # 判定为违规的
        self.paired: list[str] = []         # 模糊比较 + 同函数内有降级入口
        self.blind: dict[str, list[str]] = {"手写SQL": [], "动态取列": [], "动态filter_by": []}
        self.pair_errors: list[str] = []


def _column_ref(node: ast.AST) -> tuple[str, str] | None:
    """``Model.col`` → (模型类名, 列名)；不是这个形状返回 None。"""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id, node.attr
    return None


def _kind(model: str, attr: str) -> str | None:
    if attr in ENCRYPTED.get(model, ()):
        return "plain"
    if attr in INDEXED.get(model, ()):
        return "index"
    return None


def _enclosing_functions(tree: ast.AST) -> dict[int, ast.AST]:
    """行号 → 最内层函数节点。用来判断"同一函数里是否出现降级入口"。"""
    owner: dict[int, ast.AST] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            lineno = getattr(node, "lineno", None)
            if lineno is not None:
                owner[lineno] = fn  # 后写的是更内层（walk 自外向内）
    return owner


def _has_safe_helper(fn: ast.AST | None) -> bool:
    return fn is not None and any(h in ast.unparse(fn) for h in SAFE_HELPERS)


def _python_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


def scan() -> _Scan:
    result = _Scan()
    all_enc_names = {c for cols in ENCRYPTED.values() for c in cols}
    for path in _python_files():
        result.files += 1
        rel = path.relative_to(SERVER_DIR)
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        owner = _enclosing_functions(tree)
        is_impl = path in IMPLEMENTATION_FILES

        for node in ast.walk(tree):
            # ---- 形态一：Model.col <比较符> ...
            if isinstance(node, ast.Compare):
                for side in [node.left, *node.comparators]:
                    ref = _column_ref(side)
                    if ref is None:
                        continue
                    kind = _kind(*ref)
                    if kind is None:
                        continue
                    where = f"{rel}:{node.lineno}: {ast.unparse(node)[:100]}"
                    result.sites.append(where)
                    if kind == "index" and is_impl:
                        continue  # 实现处：索引列等值是定义本身
                    result.violations.append(
                        f"{where}\n      → {'明文加密列等值比较' if kind == 'plain' else '索引列裸等值'}"
                        f"；应走 {'pii_filter' if kind == 'plain' else 'pii_index_match'}"
                    )

            # ---- 形态二：Model.col.<方法>(...)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                ref = _column_ref(node.func.value)
                method = node.func.attr
                if ref is not None and _kind(*ref) is not None:
                    kind = _kind(*ref)
                    where = f"{rel}:{node.lineno}: {ast.unparse(node)[:100]}"
                    result.sites.append(where)
                    fn = owner.get(node.lineno)
                    if method in EQUALITY_METHODS:
                        result.violations.append(
                            f"{where}\n      → 加密列等值/集合比较；应走 pii_filter"
                        )
                    elif method in FUZZY_METHODS:
                        if kind == "index":
                            result.violations.append(
                                f"{where}\n      → 索引列做模糊匹配无意义（索引是定长 HMAC）"
                            )
                        elif _has_safe_helper(fn):
                            result.paired.append(where)
                        else:
                            result.violations.append(
                                f"{where}\n      → 密文态模糊匹配恒空，且同函数内没有 "
                                "pii_filter/pii_index_match 作为开态降级"
                            )

                # ---- 盲区计数：filter_by(**动态字典)
                if method == "filter_by" and any(k.arg is None for k in node.keywords):
                    result.blind["动态filter_by"].append(f"{rel}:{node.lineno}")

            # ---- 形态三：filter_by(col=...) / upsert_unique(db, Model, {"col": ...})
            if isinstance(node, ast.Call):
                result.violations.extend(_check_keyed_lookup(node, rel, all_enc_names))
                result.pair_errors.extend(_check_helper_pairing(node, rel))
                result.blind["动态取列"].extend(_check_getattr(node, rel))
                result.blind["手写SQL"].extend(_check_raw_sql(node, rel, all_enc_names))

    return result


def _check_keyed_lookup(node: ast.Call, rel, enc_names: set[str]) -> list[str]:
    """``upsert_unique(db, Patient, {"id_card": x}, …)``——键名是加密列即违规。

    `concurrency.upsert_unique` 内部走 `filter_by(**keys)`，等值比较照样命中
    不了密文。模型从第二个位置参数解析，非加密模型不误伤。
    """
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    if name != "upsert_unique" or len(node.args) < 3:
        return []
    model = node.args[1]
    if not isinstance(model, ast.Name) or model.id not in ENCRYPTED:
        return []
    keys = node.args[2]
    if not isinstance(keys, ast.Dict):
        return []
    bad = [
        k.value for k in keys.keys
        if isinstance(k, ast.Constant) and k.value in (ENCRYPTED[model.id] | enc_names)
    ]
    if not bad:
        return []
    return [
        f"{rel}:{node.lineno}: upsert_unique({model.id}, keys={bad})\n"
        f"      → 键含加密列，内部 filter_by 等值命中不了密文；应先用 pii_filter 查再分支"
    ]


def _check_helper_pairing(node: ast.Call, rel) -> list[str]:
    """`pii_filter(索引列, 明文列, …)` / `pii_index_match(索引列, …)` 的参数配对。"""
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    if name not in SAFE_HELPERS or not node.args:
        return []
    first = _column_ref(node.args[0])
    if first is None:
        return []
    model, idx_col = first
    if idx_col not in INDEXED.get(model, ()):
        return [
            f"{rel}:{node.lineno}: {name} 的第一个参数 {model}.{idx_col} 不是检索索引列"
        ]
    if name == "pii_index_match" or len(node.args) < 2:
        return []
    second = _column_ref(node.args[1])
    if second is None:
        return []
    if (second[0], f"{second[1]}_idx") != (model, idx_col):
        return [
            f"{rel}:{node.lineno}: pii_filter 的索引列 {model}.{idx_col} 与明文列 "
            f"{second[0]}.{second[1]} 不是同一列——写串了会静默查不到"
        ]
    return []


def _check_getattr(node: ast.Call, rel) -> list[str]:
    if not isinstance(node.func, ast.Name) or node.func.id != "getattr" or not node.args:
        return []
    target = node.args[0]
    if isinstance(target, ast.Name) and target.id in ENCRYPTED:
        return [f"{rel}:{node.lineno}"]
    return []


def _check_raw_sql(node: ast.Call, rel, enc_names: set[str]) -> list[str]:
    """`text("… WHERE id_card = :x")` 一类手写 SQL——AST 看不进字符串，只计数。"""
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    if name != "text" or not node.args:
        return []
    arg = node.args[0]
    if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
        return []
    sql = arg.value
    if any(f"{c}" in sql for c in enc_names) and any(
        kw in sql.upper() for kw in ("SELECT", "UPDATE", "DELETE", "WHERE")
    ):
        return [f"{rel}:{node.lineno}"]
    return []


RESULT = scan()


def _report() -> str:
    blind_total = sum(len(v) for v in RESULT.blind.values())
    lines = [
        "",
        "[PII 检索点守卫] 覆盖面自证",
        f"  清单来源：模型元数据（EncryptedPII 列类型），非手写。"
        f"加密列 {sum(len(v) for v in ENCRYPTED.values())} 个"
        f"（{', '.join(f'{m}.{c}' for m in sorted(ENCRYPTED) for c in sorted(ENCRYPTED[m]))}）"
        f"；配套索引列 {sum(len(v) for v in INDEXED.values())} 个",
        f"  扫描范围：{RESULT.files} 个 .py 文件（app/ + scripts/，全量，无抽样、无跳过）",
        f"  认出的检索点：{len(RESULT.sites)} 处 = 违规 {len(RESULT.violations)}"
        f" + 关态模糊·开态已降级 {len(RESULT.paired)} 处",
        f"  参数配对错误：{len(RESULT.pair_errors)} 处",
        f"  认不出的形态（声明的盲区）：{blind_total} 处 —— "
        + "、".join(f"{k} {len(v)}" for k, v in RESULT.blind.items()),
    ]
    return "\n".join(lines)


def test_覆盖面自证():
    """把"看了多少"打印出来——不声张覆盖范围的绿灯和假装看过全部的哨兵一样危险。"""
    summary = _report()
    print(summary)
    # -q 下 print 会被吞掉；warning 进 warnings summary，覆盖面数字在 CI 默认输出里也看得见。
    warnings.warn(summary, UserWarning, stacklevel=2)
    assert RESULT.files > 0 and ENCRYPTED, "扫描范围为空 = 这道闸门什么也没守"


def test_加密列都有配套的检索索引列():
    """加密列没有 `*_idx` 旁列 = 等值检索无处可去，开关一开该列就检索失明。"""
    missing = []
    for model, cols in ENCRYPTED.items():
        present = {c.name for c in MAPPED[model].__table__.columns}
        missing += [f"{model}.{c}" for c in sorted(cols) if f"{c}_idx" not in present]
    assert missing == [], (
        f"以下加密列缺少配套的检索索引列 `<列名>_idx`：{missing}。"
        " 没有索引列，开态等值检索无路可走（见 app/pii.py）。"
    )


def test_加密列的等值与模糊检索必须走pii_filter():
    assert RESULT.violations == [], (
        "以下检索点直接比较 PII 加密列——开关打开后**不报错、只是查不到**，"
        "后果是重复建档/重复开户/实名绑不上（见 app/pii.py 与 tests/test_pii_index_integrity.py）：\n  "
        + "\n  ".join(RESULT.violations)
        + "\n\n等值一律 pii_filter(Model.col_idx, Model.col, value)；"
        "模糊比较只能作为关态分支，开态必须有 pii_filter/pii_index_match 降级。"
    )


def test_pii_filter的索引列与明文列必须配对():
    assert RESULT.pair_errors == [], (
        "以下调用把索引列与明文列写串了（一样是静默查不到）：\n  "
        + "\n  ".join(RESULT.pair_errors)
    )


def test_模糊检索的降级配对没有腐烂():
    """现状三处关态模糊比较都配了开态降级。这条盯住"配对数不许凭空消失"——

    降级分支被删掉而模糊比较还在，`test_加密列的等值与模糊检索必须走pii_filter`
    会先红；这里额外把数量钉住，防的是反向的腐烂：有人把降级删了、顺手把模糊
    比较也删了，于是关态的检索行为悄悄变窄却没人发现。
    """
    assert len(RESULT.paired) >= 3, (
        f"关态模糊检索的降级配对从 3 处降到 {len(RESULT.paired)} 处："
        f"{RESULT.paired}。少掉的那处是不是把关态行为也一并改了？"
    )
