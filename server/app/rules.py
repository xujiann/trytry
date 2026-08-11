"""统一规则条件 DSL（T5.1）。

平台里已经有四套各自实现的规则：审方规则、数据质控规则、病历质控规则、
绩效指标。它们的判定逻辑写死在各自路由里，新增一类判断就要改一次代码。

这里提供统一的**条件表达式**求值：在 `app/formula.py` 的 AST 白名单基础上
扩展比较运算、布尔运算与 `in`，返回真假而不是数值。放行的语法：

- 比较：`>` `>=` `<` `<=` `==` `!=`，支持链式（`0 < x < 10`）
- 布尔：`and` `or` `not`
- 成员：`x in ("a", "b")`（字符串枚举判断）
- 算术与白名单函数：沿用 formula 的那一套
- 字符串常量与 `len()`（质控里判"字数少于 N"要用）

同样禁止属性访问、下标、推导式、lambda 与任意调用——规则由管理员录入，
和绩效公式一样属于"用户往服务端塞代码"，必须按不可信输入对待。
"""
import ast
import operator

from .formula import MAX_EXPRESSION_LENGTH, FormulaError, _eval_node

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


def _eval_operand(node: ast.AST, variables: dict):
    """求一个操作数：字符串/元组按原样返回，其余交给数值求值器。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_eval_operand(e, variables) for e in node.elts)
    if isinstance(node, ast.Name) and node.id in variables:
        value = variables[node.id]
        # 字符串变量直接返回，不强转成 float
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
        if len(node.args) != 1:
            raise RuleError("len() 只接受一个参数")
        return float(len(_eval_operand(node.args[0], variables) or ""))
    return _eval_node(node, variables)


def _eval_condition(node: ast.AST, variables: dict) -> bool:
    if isinstance(node, ast.BoolOp):
        values = [_eval_condition(v, variables) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_condition(node.operand, variables)

    if isinstance(node, ast.Compare):
        left = _eval_operand(node.left, variables)
        for op, comparator in zip(node.ops, node.comparators):
            op_type = type(op)
            if op_type not in _COMPARE_OPS:
                raise RuleError("不支持的比较运算符")
            right = _eval_operand(comparator, variables)
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


def validate_condition(expression: str, known_variables: dict) -> None:
    """录入时校验：用各变量的样例值代入试算一次。"""
    evaluate_condition(expression, known_variables)
