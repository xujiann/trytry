"""统一规则条件 DSL（T5.1）。

平台里已经有四套各自实现的规则：审方规则、数据质控规则、病历质控规则、
绩效指标。它们的判定逻辑写死在各自路由里，新增一类判断就要改一次代码。

这里提供统一的**条件表达式**求值：在 `app/formula.py` 的 AST 白名单基础上
扩展比较运算、布尔运算与 `in`，返回真假而不是数值。放行的语法：

- 比较：`>` `>=` `<` `<=` `==` `!=`，支持链式（`0 < x < 10`）
- 布尔：`and` `or` `not`
- 成员：`x in ("a", "b")`（字符串枚举判断）；单个取值写成 `("a",)` 或用 `==`——`x in ("a")` 的括号不成元组，
  成了子串判断，录入时拒收（P2-1735）；`"a" in x`（文本里含不含某段字）照收
- 算术与白名单函数：沿用 formula 的那一套
- 字符串常量与 `len()`（质控里判"字数少于 N"要用）

同样禁止属性访问、下标、推导式、lambda 与任意调用——规则由管理员录入，
和绩效公式一样属于"用户往服务端塞代码"，必须按不可信输入对待。
"""
import ast
import operator

from .formula import _ARITY, _FUNCTIONS, MAX_EXPRESSION_LENGTH, FormulaError, _eval_node

_COMPARE_OPS = {
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


class RuleError(FormulaError):
    """条件表达式非法。"""


def _coerce(name: str, value):
    """把变量值收敛成求值器认识的三种类型之一，否则抛 RuleError。

    T6.1 整改：`/api/rules/evaluate` 的 variables 是**客户端自由传入的 dict**，
    里面可能出现 null、嵌套对象、数组。此前这些值会一路走到 `float()`，
    抛出的 TypeError 不属于 RuleError，穿透调用方的兜底逻辑变成 500。
    类型不对是调用方的输入问题，应当和"引用未知变量"一样按规则级错误处理。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except OverflowError:
            # 几百位的整数 float() 不下（P2-339）
            raise RuleError(f"变量 {name} 的值超出数值范围") from None
    raise RuleError(f"变量 {name} 的值类型不受支持：{type(value).__name__}")


def _eval_operand(node: ast.AST, variables: dict, lenient: bool = False):
    """求一个操作数：字符串/元组按原样返回，其余交给数值求值器。

    `lenient` 只给录入校验逐环查链式比较用（P2-1736）：这组样例值下算不出有限实数的记成 nan 接着往下查，与公式录入校验
    （`formula.validate`）同一个道理。求值一律不带它。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_eval_operand(e, variables, lenient) for e in node.elts)
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise RuleError(f"未知变量：{node.id}")
        return _coerce(node.id, variables[node.id])
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
        if len(node.args) != 1:
            raise RuleError("len() 只接受一个参数")
        target = _eval_operand(node.args[0], variables, lenient)
        if not isinstance(target, str):
            # len(数值) 在 Python 里是 TypeError，这里转成规则级错误
            raise RuleError("len() 只能作用于文本变量")
        return float(len(target))
    return _eval_node(node, variables, lenient)


#: 比较前数值取整到的小数位：与公式求值器 `formula.evaluate` 的结果同一精度（P2-892）
_COMPARE_DIGITS = 4


def _comparable(value):
    """数值（含 `in` 右侧元组里的数）按公式求值器同一精度取整再比（P2-892）。

    原先拿原始浮点直接比：29/100×100 = 28.999999999999996，条件 `referrals_up / encounters * 100 >= 29` 在恰好 29%
    时判不中，57、58 同样不中，7、14 却中——恰好等于门槛的机构命中与否取决于具体数字；公式求值器算同一个式子得 29.0
    （先取 4 位小数），慢专病考核就是拿它和目标比。与 Westgard 的 z 恰为 2.0（P2-156）同一类毛病。小于万分之一的门槛
    在这个精度下分不出来，与公式同一个口径。文本与布尔原样。
    """
    if isinstance(value, float):
        return round(value, _COMPARE_DIGITS)
    if isinstance(value, tuple):
        return tuple(_comparable(item) for item in value)
    return value


def _eval_condition(node: ast.AST, variables: dict) -> bool:
    if isinstance(node, ast.BoolOp):
        values = [_eval_condition(v, variables) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_condition(node.operand, variables)

    if isinstance(node, ast.Compare):
        left = _comparable(_eval_operand(node.left, variables))
        for op, comparator in zip(node.ops, node.comparators):
            op_type = type(op)
            if op_type not in _COMPARE_OPS:
                raise RuleError("不支持的比较运算符")
            right = _comparable(_eval_operand(comparator, variables))
            try:
                if not _COMPARE_OPS[op_type](left, right):
                    return False
            except TypeError:
                raise RuleError(f"类型不可比较：{left!r} 与 {right!r}") from None
            left = right  # 链式比较：0 < x < 10
        return True

    # 裸变量/裸表达式按真值判断（如 `critical`）
    return bool(_eval_operand(node, variables))


def evaluate_condition(expression: str, variables: dict) -> bool:
    """求值条件表达式；非法或引用未知变量时抛 RuleError。"""
    if not expression or len(expression) > MAX_EXPRESSION_LENGTH:
        raise RuleError(f"条件长度须在 1~{MAX_EXPRESSION_LENGTH} 之间")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise RuleError(f"条件语法错误：{exc.msg}") from None
    try:
        return _eval_condition(tree.body, variables)
    except FormulaError as exc:
        raise RuleError(str(exc)) from None
    except (TypeError, ValueError, ArithmeticError) as exc:
        # 兜底：变量来自不可信输入，此函数对外只承诺抛 RuleError。
        # 上面已逐类收敛，这一层保证契约不被将来新增的分支破坏
        # （原先漏了 ArithmeticError：`daily_dose ** -1` 遇 0 剂量 ZeroDivisionError 直接 500，P2-339）。
        raise RuleError(f"条件求值失败：{exc}") from None


def _single_string_membership(node: ast.Compare) -> tuple[str, str, str] | None:
    """`变量 in ("A")`：括号里只有一个字符串时不成元组，`in` / `not in` 成了子串判断（P2-1735）。返回 (变量, 运算符, 字符串)。

    模块 docstring 把成员运算定义为字符串枚举判断，可 `drug_code in ("METFORMIN")` 在 drug_code 为 "MET"、为空串时都命中；
    拦截级的 `diagnosis_name in ("妊娠期高血压")` 命中「高血压」与空诊断。只看「左边是变量、右边是单个字符串常量」这一种：
    「字面量 in 变量」是有意的包含判断（`"高血压" in diagnosis_name`），照收。
    """
    operands = [node.left, *node.comparators]
    for op, left, right in zip(node.ops, operands, operands[1:]):
        if (isinstance(op, (ast.In, ast.NotIn)) and isinstance(left, ast.Name)
                and isinstance(right, ast.Constant) and isinstance(right.value, str)):
            return left.id, "not in" if isinstance(op, ast.NotIn) else "in", right.value
    return None


#: 条件里能调用的函数：公式求值器的白名单（min / max / round / abs），加上本模块自己认的 `len`（见 `_eval_operand`）
_CALLABLE = (*_FUNCTIONS, "len")


def _check_call(node: ast.Call) -> None:
    """函数名与参数个数按白名单静态核（P2-1736）：判据与求值时同一份（`formula._FUNCTIONS` / `_ARITY`，`len` 只收一个
    参数），不靠样例值算到它——链式比较短路掉的那一截里写错的函数名、参数个数，试算从来算不到。"""
    if not isinstance(node.func, ast.Name) or node.func.id not in _CALLABLE:
        raise RuleError(f"只允许调用 {' / '.join(_CALLABLE)}")
    if node.keywords:
        raise RuleError("函数调用不支持关键字参数")
    name, count = node.func.id, len(node.args)
    if name == "len":
        if count != 1:
            raise RuleError("len() 只接受一个参数")
        return
    if not count:
        raise RuleError("函数调用至少需要一个参数")
    low, high = _ARITY[name]
    if count < low:
        raise RuleError(f"{name} 至少需要 {low} 个参数")
    if high is not None and count > high:
        raise RuleError(f"{name} 最多接受 {high} 个参数")


def _check_every_link(node: ast.Compare, variables: dict) -> None:
    """链式比较逐环都算、两两查可比性，不短路（P2-1736）。求值语义不动：`_eval_condition` 照旧一环为假即停。

    样例值下算不出有限实数的记成 nan 接着往下查（`lenient`）：那是取值问题不是写错，换组真实取值就算得出，与公式录入
    校验同一个道理；类型对不上（数与文本比大小、`len(数值)`）不随取值变，照样报。"""
    values = [_comparable(_eval_operand(operand, variables, lenient=True)) for operand in (node.left, *node.comparators)]
    for op, left, right in zip(node.ops, values, values[1:]):
        if type(op) not in _COMPARE_OPS:
            raise RuleError("不支持的比较运算符")
        try:
            _COMPARE_OPS[type(op)](left, right)
        except TypeError:
            raise RuleError(f"类型不可比较：{left!r} 与 {right!r}") from None


def validate_condition(expression: str, known_variables: dict) -> None:
    """录入时校验：用各变量的样例值代入试算一次，再把表达式里引用的变量逐个对一遍（P2-352）。

    光试算不够：链式比较一环为假就不再往下算——`65 <= age < max_agee` 在样例 age=40 时第一环就是假，写错的 `max_agee`
    从没被求值，录入照收；上线后每一次真求值（age ≥ 65）都记一条「未知变量」错误，规则形同虚设。

    P2-352 只补了变量名这一半（P2-1736）：后半截里的引号数字（`65 <= age < "80"`）、写错的函数名（`maxx(age, 80)`）、
    `len(数值)`、参数个数不对（`max(age)`）照样录得进去，age ≥ 65 时每次求值都报错。函数名与参数个数按白名单静态核
    （`_check_call`），每个比较逐环都算、两两查可比性（`_check_every_link`）。

    「变量 in 单个字符串」录入时拒收（P2-1735，见 `_single_string_membership`）：试算查不出来——子串判断不报错，只是
    结果与枚举判断不一样。只拦录入，求值语义不动（已录入的照旧按子串算，存量要人工改写）。
    """
    evaluate_condition(expression, known_variables)
    tree = ast.parse(expression, mode="eval")
    functions = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and id(node) not in functions and node.id not in known_variables:
            raise RuleError(f"未知变量：{node.id}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            _check_call(node)
    try:
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                _check_every_link(node, known_variables)
    except FormulaError as exc:
        raise RuleError(str(exc)) from None
    except (TypeError, ValueError, ArithmeticError) as exc:
        # 与 `evaluate_condition` 同一层兜底：对外只抛 RuleError（路由只接它，漏出别的就是 500）
        raise RuleError(f"条件求值失败：{exc}") from None
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and (hit := _single_string_membership(node)):
            name, op, value = hit
            raise RuleError(f'{name} {op} ("{value}") 的括号里只有一个字符串、不成元组，会按子串判断（它的任意一段、空串'
                            f'都算在内）：单个取值写成 ("{value}",) 或用 {"!=" if op == "not in" else "=="}')
