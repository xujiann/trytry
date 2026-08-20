"""受限表达式求值器（T4.3）。

绩效公式由管理员录入，等同于让用户往服务端塞代码——用 `eval()` 就是把远程
执行漏洞写进产品。这里走 AST 白名单：解析成语法树后逐节点检查，只放行

- 数字字面量
- 变量名（必须在传入的变量表里）
- 四则运算、取模、幂、一元正负
- 括号
- 少量白名单函数：min / max / round / abs

其余一律拒绝——属性访问（`().__class__`）、下标、函数定义、推导式、
lambda、任何未白名单的调用。除零返回 0 而不是抛异常：绩效公式里分母为零
是常态（某机构本期没有业务量），报错会让整张报表出不来。
"""
import ast
import operator
from typing import Any, Callable

MAX_EXPRESSION_LENGTH = 512
# 幂运算的指数上限：2**999999 能把进程算到内存耗尽
MAX_POWER_EXPONENT = 8

_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos, ast.USub: operator.neg,
}
_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "min": min, "max": max, "round": round, "abs": abs,
}


class FormulaError(ValueError):
    """表达式非法或引用了未知变量。"""


def _eval_node(node: ast.AST, variables: dict[str, float]) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError("表达式只允许数字常量")
        return float(node.value)

    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise FormulaError(f"未知变量：{node.id}")
        value = variables[node.id]
        # T6.1 整改：变量值可能来自客户端（规则求值接口），null/对象/数组
        # 走到 float() 会抛 TypeError，穿透调用方的兜底变成 500
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise FormulaError(f"变量 {node.id} 的值不是数值：{type(value).__name__}")
        try:
            return float(value)
        except ValueError:
            raise FormulaError(f"变量 {node.id} 的值不是数值：{value!r}") from None

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BIN_OPS:
            raise FormulaError("不支持的运算符")
        left = _eval_node(node.left, variables)
        right = _eval_node(node.right, variables)
        if op_type in (ast.Div, ast.Mod) and right == 0:
            # 分母为零在绩效公式里是常态（本期无业务量），返回 0 而不是让整张报表出不来
            return 0.0
        if op_type is ast.Pow and abs(right) > MAX_POWER_EXPONENT:
            raise FormulaError(f"幂指数不得超过 {MAX_POWER_EXPONENT}")
        return float(_BIN_OPS[op_type](left, right))

    if isinstance(node, ast.UnaryOp):
        unary_type = type(node.op)
        if unary_type not in _UNARY_OPS:
            raise FormulaError("不支持的一元运算符")
        return float(_UNARY_OPS[unary_type](_eval_node(node.operand, variables)))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise FormulaError("只允许调用 min / max / round / abs")
        if node.keywords:
            raise FormulaError("函数调用不支持关键字参数")
        args = [_eval_node(a, variables) for a in node.args]
        if not args:
            raise FormulaError("函数调用至少需要一个参数")
        if node.func.id == "round" and len(args) > 1:
            # round 的第二参数必须是整数；求值器把一切都转成 float，这里转回去
            if args[1] != int(args[1]):
                raise FormulaError("round 的小数位数必须是整数")
            args = [args[0], int(args[1])]
        return float(_FUNCTIONS[node.func.id](*args))

    raise FormulaError(f"表达式包含不允许的语法：{type(node).__name__}")


def evaluate(expression: str, variables: dict[str, float]) -> float:
    """求值；表达式非法或引用未知变量时抛 FormulaError。"""
    if not expression or len(expression) > MAX_EXPRESSION_LENGTH:
        raise FormulaError(f"表达式长度须在 1~{MAX_EXPRESSION_LENGTH} 之间")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"表达式语法错误：{exc.msg}") from None
    return round(_eval_node(tree.body, variables), 4)


def validate(expression: str, known_variables: set[str]) -> None:
    """录入时校验：语法合法且只引用已知变量。用哑值代入，不触发实际计算。"""
    evaluate(expression, {name: 1.0 for name in known_variables})
