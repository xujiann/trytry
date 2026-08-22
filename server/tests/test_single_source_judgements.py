"""一个判定只许有一份实现——静态守卫。

## 为什么有这个文件

`app/ws.py` 的 `_token_valid()` 曾是 HTTP 侧准入判定的**第二份、更弱的拷贝**：
验签名/过期/黑名单，却漏了账号停用、改密基线、令牌作用域三项。同一枚令牌
HTTP 侧 403/401、WebSocket 侧照常建连收危急值。它不是"代码写重了"，
而是**同一个业务问题有了两个可以各自演化的答案**——HTTP 侧补了三条判定，
WS 侧不会跟着补，谁也不会发现。

修法是抽 `deps.check_token_admission` 让两侧共用唯一实现。但"某处修好了"
不等于"守得住"：下一个人照样可以在第三处再写一份。本文件是那道闸门。

## 判据（与本轮排查同一把尺）

不是"代码相似"就算重复。要害是**改了一处、另一处不会跟**。所以每条扫描
盯的都是一个**具体的判定**，而不是一种写法：

| # | 判定 | 唯一实现 |
|---|---|---|
| 1 | 一枚令牌能否代表一个可用账号 | `deps.check_token_admission` |
| 2 | 登出黑名单用什么键 | `security.revocation_key` |
| 3 | 档案调阅授权此刻是否有效 | `visibility.active_authorization_grants` |
| 4 | 患者可见性判定与留痕（绑死的一次动作） | `visibility._write_access_log` |
| 5 | PII 列（证件号/手机号）如何进查询 | `pii.pii_filter` / `pii.pii_index_match` |
| 6 | 一个批次还能发多少 | `dispense.batch_available` |
| 7 | 押金余额是多少 | `billing.deposit_balance` |

## 豁免清单只减不增

收敛不了的（口径**确实该不同**）写进 `EXEMPTIONS`，每条带理由。加一条豁免
就是在账上多记一笔债，评审时看得见；删一条不需要任何人批准。

## 闸门自证覆盖面

`test_守卫自证覆盖面` 打印扫了多少文件、多少个函数、每类判定**查到几处落脚点**、
豁免几条。上一轮的教训是"报着 100% 覆盖率、实际只扫了 11% 的文件"——所以这里
把分母也打出来并断言它就是 `app/` 下的全部 `.py`。

落脚点数不是装饰：AST 形状写歪的扫描同样得到"0 违规 + 100% 覆盖率"。
本文件第一版的 PII 那条就是这么被自己抓住的——它盯"裸等值比较"，
而收敛后的代码里那种写法一处都没有，于是它永远不会响。现在每类判定的
落脚点数为 0 即断言失败，扫描器空转当场暴露。
"""
import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"

#: 扫描面 = app/ 下全部 .py（含 spd 子系统）。分母显式算出来，不靠印象。
PY_FILES = sorted(p for p in APP_DIR.rglob("*.py"))
MODULES = {
    p.relative_to(APP_DIR.parent).as_posix(): ast.parse(p.read_text(encoding="utf-8"), str(p))
    for p in PY_FILES
}


# ---------------------------------------------------------------- 通用小工具


def _functions(tree: ast.AST):
    """(限定名, 节点)：模块级函数、类方法、嵌套函数都算一个判定的落脚点。"""
    out = []

    def walk(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                out.append((name, child))
                walk(child, prefix=f"{name}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix=f"{prefix}{child.name}.")
            else:
                walk(child, prefix=prefix)

    walk(tree)
    return out


def _model_attr_compares(tree: ast.AST, model_names: set[str], attrs: set[str]):
    """形如 `Model.attr == x` / `Model.attr >= x` 的比较（模型类名字面量匹配）。"""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if (
            isinstance(left, ast.Attribute)
            and left.attr in attrs
            and isinstance(left.value, ast.Name)
            and left.value.id in model_names
        ):
            hits.append((f"{left.value.id}.{left.attr}", node.lineno))
    return hits


def _calls(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == name:
                return True
            if isinstance(func, ast.Attribute) and func.attr == name:
                return True
    return False


def _names(tree: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


# ---------------------------------------------------------------- 豁免清单

#: key = "模块::函数"（或模块），value = 为什么它不是"第二份实现"。
#: **只减不增**：每加一条都要说清"这两个口径确实该不同"，否则就去收敛。
EXEMPTIONS = {
    "app/routers/portal.py::current_resident": (
        "居民端准入。主体是 resident_accounts 行、不在 users 表内：没有改密基线列，"
        "也没有'停用 403 / 令牌失效 401'的区分，且判定方向相反（只收 scope=portal）。"
        "合成一份只会得到一个按 scope 分叉的双主体函数。两者真正共用的那一件事"
        "——登出黑名单的键——已收敛到 security.revocation_key。"
    ),
    "app/routers/access_logs.py::_log_view": (
        "留痕而非判定。记的是'谁查了某患者的调阅记录'这个动作本身，"
        "接口已限 director/admin（全域角色），不含任何可见性判定分支。"
    ),
}


def _check(violations, scan_name):
    assert not violations, (
        f"[{scan_name}] 发现同一判定的第二份实现——请收敛到唯一实现，"
        f"或在 EXEMPTIONS 里写清为什么口径该不同：\n  "
        + "\n  ".join(violations)
    )


# ---------------------------------------------------------------- 扫描 1


#: 准入判定的四个信号。一个函数同时出现 ≥2 个，就说明它在**自己**回答
#: "这枚令牌能不能代表一个可用账号"，而不是把这个问题交给唯一实现。
ADMISSION_SIGNALS = ("blacklist", "scope", "status", "baseline")

ADMISSION_SINGLE_SOURCE = "app/deps.py::check_token_admission"


def _looks_at_a_token(fn: ast.AST) -> bool:
    """这个函数手上有没有一枚令牌/一份声明。

    没有这一道，扫描会把**写入端**误判成判定端：`users.set_user_status`
    在停用账号时推 `token_valid_from` 基线、且比较 `status == "disabled"`，
    两个信号都齐，但它根本不解析任何令牌——它是在改状态，不是在放行。
    判定端的共同特征是手上有 claims/token。
    """
    if _calls(fn, "decode_token"):
        return True
    return bool(_names(fn) & {"claims", "token"})


def _admission_signals(fn: ast.AST) -> set[str]:
    if not _looks_at_a_token(fn):
        return set()
    found = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == "revoked_tokens":
            found.add("blacklist")
        if isinstance(node, ast.Name) and node.id == "token_issued_before_baseline":
            found.add("baseline")
        if isinstance(node, ast.Attribute) and node.attr == "token_valid_from":
            found.add("baseline")
        if isinstance(node, ast.Compare):
            consts = {
                c.value
                for c in node.comparators
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            }
            left_src = ast.dump(node.left)
            if "portal" in consts and "'scope'" in left_src:
                found.add("scope")
            if (
                isinstance(node.left, ast.Attribute)
                and node.left.attr == "status"
                and consts & {"disabled", "active"}
            ):
                found.add("status")
    return found


def _admission_violations():
    bad = []
    for module, tree in MODULES.items():
        for qualname, fn in _functions(tree):
            key = f"{module}::{qualname}"
            signals = _admission_signals(fn)
            if len(signals) < 2:
                continue
            if key == ADMISSION_SINGLE_SOURCE or key in EXEMPTIONS:
                continue
            bad.append(f"{key}（第 {fn.lineno} 行）自行判定了 {sorted(signals)}")
    return bad


def test_准入判定只有一份实现():
    """任何模块都不得再出现"解码令牌 + 自行判定 status/baseline/scope/黑名单"的组合形状。

    这正是 `ws._token_valid` 当初的形状：它凑齐了签名+过期+黑名单，缺了另外三条，
    于是成了一份更弱的准入判定。闸门盯的是"凑够两个及以上信号"这件事本身——
    只要有第二处开始自己回答这个问题，不管它写得对不对，先拦下来。
    """
    _check(_admission_violations(), "准入判定")


def test_WS通道不自行判定准入():
    """点名钉住 ws.py：它是这条债的原产地，回归价值最高。

    要求两件事同时成立——不自己判（无任何自建信号），且确实调了唯一实现。
    只查前者的话，"既不判也不调"（等于不鉴权）同样能通过。
    """
    tree = MODULES["app/ws.py"]
    for qualname, fn in _functions(tree):
        signals = _admission_signals(fn)
        assert not signals, (
            f"app/ws.py::{qualname} 又开始自行判定准入 {sorted(signals)}——"
            "WS 侧必须调 deps.check_token_admission，不得再留第二份口径"
        )
    assert _calls(tree, "check_token_admission"), (
        "app/ws.py 不再调用 deps.check_token_admission：要么鉴权被摘掉了，"
        "要么判定又被搬回本模块"
    )


# ---------------------------------------------------------------- 扫描 2


def _is_revocation_key_call(node) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "revocation_key"
    )


def _blacklist_key_sites(tree: ast.AST):
    """每一处"往黑名单里写键"与"拿键查黑名单"，以及它取键的表达式。

    只盯这两个点，不盯 `.get("jti") or ...` 这种写法本身——同样的写法在
    `deps._enforce_session_idle_timeout` 里是**会话登记**的键（fallback 到 sub），
    那是另一个判定，与黑名单无关。盯写法会把它一起冤枉进来；盯取键点不会。
    """
    sites = []
    for node in ast.walk(tree):
        # `revoked_tokens.add(key, ...)`
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "revoked_tokens"
            and node.args
        ):
            sites.append(("写入", node.lineno, node.args[0]))
        # `key in revoked_tokens`
        if isinstance(node, ast.Compare) and any(
            isinstance(op, ast.In) for op in node.ops
        ):
            for comparator in node.comparators:
                if isinstance(comparator, ast.Name) and comparator.id == "revoked_tokens":
                    sites.append(("判定", node.lineno, node.left))
    return sites


def _assigned_from_revocation_key(tree: ast.AST, name: str) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and _is_revocation_key_call(node.value)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
        ):
            return True
    return False


def test_登出黑名单的键只有一份口径():
    """写入端与判定端必须同键，否则黑名单静默失效（写进去一个键、按另一个键查）。

    这条判定横跨四处、两个子系统（业务端 auth/deps、居民端 portal），
    是"两个可以各自演化的答案"的教科书形状：键规则一改，漏掉任一处，
    所有正常路径的用例照样绿，只有"登出后还能用"这件事悄悄发生。
    """
    bad = []
    seen = 0
    for module, tree in MODULES.items():
        if module == "app/security.py":
            continue
        for kind, lineno, key_expr in _blacklist_key_sites(tree):
            seen += 1
            if _is_revocation_key_call(key_expr):
                continue
            # 变量转手也认：`key = revocation_key(...)` 后再 `revoked_tokens.add(key)`
            if isinstance(key_expr, ast.Name) and _assigned_from_revocation_key(
                tree, key_expr.id
            ):
                continue
            bad.append(
                f"{module}:{lineno} 的黑名单{kind}键不是 security.revocation_key 给的"
            )
    assert seen >= 3, f"黑名单取键点只扫到 {seen} 处，扫描器可能失效"
    _check(bad, "黑名单键口径")


# ---------------------------------------------------------------- 扫描 3


def test_档案授权有效期判定只有一份实现():
    """`status=active` + 有效期这套判定只许 visibility 给出。

    收敛前的两份答案在**空 `expire_date`** 上正好相反：可见性侧当"不设到期日、
    有效"，校验接口侧在 SQL 里比 `expire_date >= 今天`、空串一律判无效。
    而这一列是 `String(10) default=""` 的非空列，空串可达——同一条长期授权，
    把门的说能调阅，给对接方看的说 allowed=false。
    """
    bad = []
    for module, tree in MODULES.items():
        if module == "app/visibility.py":
            continue
        for expr, lineno in _model_attr_compares(
            tree, {"ArchiveAuthorization"}, {"status", "expire_date"}
        ):
            bad.append(f"{module}:{lineno} 自行判定 {expr}")
    _check(bad, "档案授权有效期")


# ---------------------------------------------------------------- 扫描 4


def test_患者调阅留痕只由可见性模块写():
    """判定与留痕是同一个动作——`assert_patient_visible` 校验通过后自己写 AccessLog。

    若别处也能构造 AccessLog，就等于承认"可以先校验、再记得去记一笔"，
    而需要人记得的事就会被忘掉（visibility 模块 docstring 记着这条的由来）。
    """
    bad = []
    for module, tree in MODULES.items():
        if module in ("app/visibility.py", "app/models/core.py"):
            continue
        for qualname, fn in _functions(tree):
            if f"{module}::{qualname}" in EXEMPTIONS:
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "AccessLog"
                ):
                    bad.append(f"{module}::{qualname}:{node.lineno} 自行构造 AccessLog")
    _check(bad, "患者调阅留痕")


# ---------------------------------------------------------------- 扫描 5


#: 加密存储的 PII 列（见 `app/pii.py` 模块 docstring）与其检索助手。
PII_MODELS = {"Patient", "ResidentAccount"}
PII_COLUMNS = {"id_card", "phone", "id_card_idx", "phone_idx"}
PII_HELPERS = {"pii_filter", "pii_index_match"}


def _pii_column_uses(tree: ast.AST):
    """每一处**在查询里用到 PII 列**的地方，以及它是不是经助手用的。

    盯"用到"而不是盯"等值比较"：后者在收敛后的代码里一个落脚点都没有
    （列都作为参数传给 `pii_filter` 了），于是那条扫描永远 0 违规——
    看起来在守，其实是空转。本文件的自证断言就是这么发现它的。
    """
    allowed = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in PII_HELPERS
        ):
            for arg in node.args:
                for inner in ast.walk(arg):
                    allowed.add(id(inner))
    # 模糊检索（like/contains）是**另一个问题**，不是等值检索的第二份实现：
    # 密文列上它恒空，pii.py 的模块 docstring 把这条降级写在案——开态改走
    # 索引列全值命中。所以放行它，但要求同一个函数里确实配了那条降级
    # （只写模糊分支、不配降级，才是开关一打开就静默查不到的那种缺陷）。
    fuzzy = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("like", "contains", "ilike")
        ):
            fuzzy.add(id(node.func.value))

    uses = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in PII_COLUMNS
            and isinstance(node.value, ast.Name)
            and node.value.id in PII_MODELS
        ):
            kind = "helper" if id(node) in allowed else (
                "fuzzy" if id(node) in fuzzy else "raw"
            )
            uses.append((f"{node.value.id}.{node.attr}", node.lineno, kind))
    return uses


def _function_has_pii_helper(fn: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in PII_HELPERS
        for n in ast.walk(fn)
    )


def test_PII列检索只有一份口径():
    """证件号/手机号在查询里一律经 `pii_filter` / `pii_index_match`。

    与 ws 那份拷贝同一形状的已发现实例：裸写 `id_card_idx == ...` 绕过
    `pii_filter`，于是密钥轮换宽限期内旧钥数据检索不到——查不到不是报错，
    而是"这人没建过档"，接口顺手再建一条重复主数据、居民端重复开户。
    开关关态下裸比较照样"能跑"，两份口径于是可以一直各自演化。
    """
    bad = []
    for module, tree in MODULES.items():
        if module in ("app/pii.py", "app/models/core.py"):
            continue
        # 按函数遍历会漏掉"不在任何函数里"的模块级用法——先自查一遍扫描面，
        # 数目对不上说明有用法落在函数之外，扫描器看不见（今天为 0，将来未必）。
        covered = sum(len(_pii_column_uses(fn)) for _, fn in _functions(tree))
        assert covered == len(_pii_column_uses(tree)), (
            f"{module} 有 PII 列用法落在函数之外，本扫描看不见它"
        )
        # 按**函数**判：模糊分支要不要放行，取决于同一个函数里配没配那条降级。
        for qualname, fn in _functions(tree):
            paired = _function_has_pii_helper(fn)
            for expr, lineno, kind in _pii_column_uses(fn):
                if kind == "helper":
                    continue
                if kind == "fuzzy":
                    if paired:
                        continue  # 模糊检索 + 开态走索引列全值命中，pii.py 已写明的降级
                    bad.append(
                        f"{module}::{qualname}:{lineno} 对 {expr} 做模糊检索却没配开态降级"
                        "（密文列上 like/contains 恒空，开关一打开就静默查不到）"
                    )
                else:
                    bad.append(
                        f"{module}::{qualname}:{lineno} 直接拿 {expr} 进查询，未经 pii_filter"
                    )
    _check(bad, "PII 列检索")


# ---------------------------------------------------------------- 扫描 6


def test_批次可发量只有一份算法():
    """`quantity - used_quantity - blocked_quantity` 只许 `dispense.batch_available` 算。

    这个量同时是"能不能发药""调拨够不够""缺药要不要报警"三处的依据；
    各算一遍就会出现"汇总够、可发不够"这类对不上的账。
    """
    bad = []
    for module, tree in MODULES.items():
        for qualname, fn in _functions(tree):
            if f"{module}::{qualname}" == "app/routers/dispense.py::batch_available":
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Sub):
                    continue
                # 减法**本身**要同时扣掉这两项才算重算可发量：只是把
                # used_quantity / blocked_quantity 当字段输出（pharmacy._batch_out
                # 那样的 remaining = quantity - used_quantity）不是第二份算法。
                attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
                if {"used_quantity", "blocked_quantity"} <= attrs:
                    bad.append(f"{module}::{qualname}:{node.lineno} 自行算批次可发量")
    _check(bad, "批次可发量")


# ---------------------------------------------------------------- 扫描 7


def test_押金余额只由billing给出():
    """余额 = 预交 − 退费 − 结算冲抵，只由 `billing.deposit_balance` 现算。

    居民端曾是最容易长出第二份的地方（它要显示同一个数字）；现在它 import
    该函数（见 routers/portal.py 的 import 注释）。本条盯住的是"别的模块
    自己去加减 Deposit.amount"——两个数字对不上时没人说得清哪个是对的。
    """
    bad = []
    for module, tree in MODULES.items():
        if module == "app/routers/billing.py":
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "amount"
                and isinstance(node.value, ast.Name)
                and node.value.id == "Deposit"
            ):
                bad.append(f"{module}:{node.lineno} 自行聚合 Deposit.amount")
    _check(bad, "押金余额")


# ---------------------------------------------------------------- 闸门自证


def _scan_census() -> dict[str, tuple[int, str]]:
    """每类判定**实际查到了几个落脚点**，以及唯一实现是谁。

    只报"扫了 164 个文件"是不够的：扫描器写歪了（比如 AST 形状匹配不上）
    同样会得到 0 违规 + 100% 覆盖率。所以这里把每类判定命中的**具体点数**
    也报出来——点数掉到 0 就说明扫描器空转了，下面的断言会拦住。
    """
    admission = blacklist = authorization = access_log = 0
    pii = dispensable = deposit = 0
    for module, tree in MODULES.items():
        for _, fn in _functions(tree):
            if _admission_signals(fn):
                admission += 1
            for node in ast.walk(fn):
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
                    attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
                    if {"used_quantity", "blocked_quantity"} <= attrs:
                        dispensable += 1
        blacklist += len(_blacklist_key_sites(tree))
        authorization += len(
            _model_attr_compares(tree, {"ArchiveAuthorization"}, {"status", "expire_date"})
        )
        pii += len(_pii_column_uses(tree))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AccessLog"
            ):
                access_log += 1
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "amount"
                and isinstance(node.value, ast.Name)
                and node.value.id == "Deposit"
            ):
                deposit += 1
    return {
        "准入判定（令牌 → 可用账号）": (admission, "deps.check_token_admission"),
        "登出黑名单键口径": (blacklist, "security.revocation_key"),
        "档案授权有效期": (authorization, "visibility.active_authorization_grants"),
        "患者调阅留痕": (access_log, "visibility._write_access_log"),
        "PII 列检索": (pii, "pii.pii_filter / pii_index_match"),
        "批次可发量": (dispensable, "dispense.batch_available"),
        "押金余额": (deposit, "billing.deposit_balance"),
    }


SCANS = tuple(
    (
        "准入判定（令牌 → 可用账号）",
        "登出黑名单键口径",
        "档案授权有效期",
        "患者调阅留痕",
        "PII 列检索",
        "批次可发量",
        "押金余额",
    )
)


def test_守卫自证覆盖面():
    """闸门必须自己说清扫了什么——上一轮的教训是"报 100%、实际只扫了 11%"。

    所以这里把**分母**也算出来并断言：扫描面就是 `app/` 下的全部 `.py`，
    没有任何目录被悄悄漏掉（spd 子系统尤其容易漏——它是独立包）。
    """
    on_disk = {p.relative_to(APP_DIR.parent).as_posix() for p in APP_DIR.rglob("*.py")}
    scanned = set(MODULES)
    assert scanned == on_disk, f"扫描面与磁盘不一致，漏扫：{sorted(on_disk - scanned)}"

    total_functions = sum(len(_functions(t)) for t in MODULES.values())
    spd_files = [m for m in scanned if m.startswith("app/spd/")]
    router_files = [m for m in scanned if "/routers/" in m]

    print("\n" + "=" * 68)
    print("单一判定源守卫 · 覆盖面自证")
    print("=" * 68)
    print(f"  扫描文件：{len(scanned)} / {len(on_disk)} 个 app/ 下的 .py = "
          f"{len(scanned) / len(on_disk) * 100:.0f}%（含 spd 子系统 {len(spd_files)} 个、"
          f"路由 {len(router_files)} 个）")
    print(f"  扫描函数：{total_functions} 个（模块级函数 + 类方法 + 嵌套函数）")
    print(f"  准入信号：{len(ADMISSION_SIGNALS)} 个 —— " + "、".join(ADMISSION_SIGNALS))
    census = _scan_census()
    print(f"  判定类别：{len(census)} 类（落脚点数 → 唯一实现）")
    for name, (count, owner) in census.items():
        print(f"    - {name}：查到 {count} 处落脚点 → {owner}")
    print(f"  豁免条目：{len(EXEMPTIONS)} 条（只减不增，每条带理由）")
    for key, reason in EXEMPTIONS.items():
        print(f"    - {key}\n        {reason.splitlines()[0]}")
    print("=" * 68)

    assert len(scanned) == len(on_disk)
    assert total_functions > 1000, "函数扫描面异常偏小，检查 _functions 是否漏了嵌套结构"
    assert set(census) == set(SCANS)
    empty = [name for name, (count, _) in census.items() if count == 0]
    assert not empty, (
        f"这些扫描一个落脚点都没查到，说明它在空转（AST 形状没匹配上就永远 0 违规）：{empty}"
    )
