"""按「是不是数字」判完就转换：只认 ASCII 数字，别让 `isdigit()` 放行的字符在下一步炸掉（P1-97）。

`str.isdigit()` / `isdecimal()` / `isnumeric()` 认的不只是 0-9：全角「１２３」、上标「²」、圈码「①」、
阿拉伯-印度数字都算。放行之后的下一步各有各的死法（2026-09-24 实测，修前代码）：

- `int("²")` / `int("①")` 抛 `ValueError`——HL7 ORU 的 OBR 申请单号、FHIR DiagnosticReport 引用的申请单号、
  FHIR Encounter 的 `serviceProvider` 机构号。入站由 `_run_inbound` 兜底成 422「消息解析失败」，不是 500，但真正的原因
  被吞掉了：对方系统看不出是编号不对，照着「解析失败」去查报文格式；
- `hmac.compare_digest` 碰到非 ASCII 抛 `TypeError`——动态口令填全角「１２３４５６」，登录（口令已对）与
  开通动态口令的确认都是 **500**；
- 数据质控的身份证校验：前 17 位带上标数字，整条规则一跑就 **500**（一条脏数据拖垮整轮质控）；全角数字的
  证件号反倒被判为**合法**（`int("１")` 能转）；
- 存量导入的处方天数：`①` 过了 `isdigit`，`int()` 抛异常，**整个导入中断**，错误行明细也不落盘。

修法：判断改成 `s.isascii() and s.isdigit()`。闸门：`app/` 与 `scripts/` 里 `isdigit()` / `isdecimal()` /
`isnumeric()` 的调用，同一个布尔表达式里必须有同一对象的 `isascii()`，零基线；按设计的豁免写明理由，只减不增。
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 零基线（`scripts/dump_gate_status.py` 把它列进闸门现状）。2026-09-24 实测 10 处：入站 3（HL7 申请单号、FHIR
#: 申请单号 / 机构号）、对接水位 1、身份证质控 1、慢专病模板版本号 1、启动配置 1、导入天数 1 同一批改完；动态口令与
#: 支付回调验签 2 处在认证 / 验签路径上，按 §8 待复核（P1-99），暂列豁免。
BASELINE = 0

#: 按设计不配 isascii 的调用：`文件:函数` → 理由。只减不增。
BY_DESIGN = {
    "app/routers/integration.py:parse_hl7v2_patient": "入站出生日期的口径待裁定（P1-61 余项），这里只拼字符串、不转换，不在本条修",
    "app/config.py:_char_classes": "口令 / 密钥复杂度只问「含不含数字」这一字符类，不做转换",
    "app/security.py:validate_password_strength": "同上：复杂度规则的字符类判断，不做转换",
    # 下面两处是认证 / 验签路径：改法（失败关闭）按 CLAUDE.md §8 待复核，见待裁定清单 P1-99，修完划掉
    "app/totp.py:verify": "动态口令校验，认证路径——待复核（P1-99）",
    "app/egress.py:verify_signature": "支付网关回调验签——待复核（P1-99）",
}

_DIGIT_CHECKS = ("isdigit", "isdecimal", "isnumeric")


def _enclosing_functions(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def unguarded_digit_checks(sources: dict[str, str] | None = None) -> list[str]:
    """`文件:函数`——调了 `x.isdigit()` 一类，同一个布尔表达式里却没有 `x.isascii()`。"""
    if sources is None:
        sources = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8")
                   for base in ("app", "scripts") for p in sorted((ROOT / base).rglob("*.py"))
                   if "__pycache__" not in p.parts}
    found = set()
    for name, text in sources.items():
        tree = ast.parse(text)
        parents = _enclosing_functions(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _DIGIT_CHECKS and not node.args):
                continue
            target = ast.unparse(node.func.value)
            guarded = False
            cur = parents.get(node)
            while cur is not None and not isinstance(cur, (ast.stmt,)):
                if isinstance(cur, ast.BoolOp) and any(
                        isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "isascii"
                        and ast.unparse(v.func.value) == target for v in ast.walk(cur)):
                    guarded = True
                    break
                cur = parents.get(cur)
            if guarded:
                continue
            fn = parents.get(node)
            while fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = parents.get(fn)
            found.add(f"{name}:{fn.name if fn is not None else '<module>'}")
    return sorted(found)


# ================================================================ 闸门
def test_数字判断只认ASCII():
    offenders = [x for x in unguarded_digit_checks() if x not in BY_DESIGN]
    assert len(offenders) <= BASELINE, (
        "`isdigit()` / `isdecimal()` / `isnumeric()` 放行全角、上标、圈码数字，下一步 int() / compare_digest 就抛异常。"
        f"改成 `x.isascii() and x.isdigit()`，确实只判字符类的写进 BY_DESIGN 并写明理由：{offenders}"
    )


def test_豁免名单都还在用():
    found = set(unguarded_digit_checks())
    stale = [k for k in BY_DESIGN if k not in found]
    assert stale == [], f"这些豁免已经用不上了（函数改名或已配 isascii），请划掉：{stale}"


def test_判据自证_裸isdigit点名_配了isascii的不报():
    snippet = '''
def bare(x):
    return int(x) if x.isdigit() else 0

def guarded(x):
    return int(x) if x.isascii() and x.isdigit() else 0

def negated(x):
    if not (x.isascii() and x.isdigit()):
        return 0
    return int(x)

def other_target(x, y):
    return y.isascii() and x.isdigit()

def char_class(s):
    return any(c.isdigit() for c in s)
'''
    assert unguarded_digit_checks({"自证.py": snippet}) == [
        "自证.py:bare", "自证.py:char_class", "自证.py:other_target",
    ]


# ================================================================ 行为回归（修前实测见 docstring）
def test_身份证质控_全角判不合法_上标不再让整条规则抛异常():
    from app.routers.dataquality import id_card_invalid_reason

    assert id_card_invalid_reason("１１０１０５１９４９１２３１００２X") == "身份证号前17位应全为数字"
    assert id_card_invalid_reason("1101051949123100²2") == "身份证号前17位应全为数字"
    assert id_card_invalid_reason("11010519491231002X") == ""


def test_HL7_ORU申请单号是上标数字_报的是编号不对而不是解析失败(client, admin):
    message = "\r".join([
        "MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20260821100000||ORU^R01|P197A|P|2.4",
        "PID|1||110101199001011234^^^CN^ID||解析回归",
        "OBR|1|²||GLU^血糖组合",
        "OBX|1|NM|GLU^空腹血糖|1|5.0|mmol/L|3.9-6.1|",
    ])
    resp = client.post("/api/integration/hl7v2/oru", json={"message": message}, headers=admin)
    # 修前是兜底的「消息解析失败」：int("²") 抛异常被 `_run_inbound` 吞成 422，原因看不出来
    assert resp.status_code == 422 and "申请单号" in resp.json()["detail"], (resp.status_code, resp.text[:200])


def test_FHIR_申请单号与机构号是圈码数字_报的是编号不对而不是解析失败(client, admin):
    report = {"resourceType": "DiagnosticReport", "status": "final",
              "basedOn": [{"reference": "ServiceRequest/①"}], "conclusion": "解析回归"}
    resp = client.post("/api/integration/fhir/DiagnosticReport", json=report, headers=admin)
    assert resp.status_code == 422 and "申请单号" in resp.json()["detail"], (resp.status_code, resp.text[:200])
    patient = client.post("/api/patients", headers=admin, json={
        "name": "解析回归", "id_card": "110101199001019977", "gender": "男", "birth_date": "1990-01-01"})
    assert patient.status_code in (200, 201), patient.text
    encounter = {"resourceType": "Encounter", "class": {"code": "AMB"},
                 "subject": {"reference": f"Patient/{patient.json()['ehc_no']}"},
                 "serviceProvider": {"reference": "Organization/①"}}
    resp = client.post("/api/integration/fhir/Encounter", json=encounter, headers=admin)
    assert resp.status_code == 422 and "serviceProvider" in resp.json()["detail"], (resp.status_code, resp.text[:200])


def test_慢专病模板版本号带上标数字_复制照常出新版本而不是500(client, admin):
    program = client.get("/api/spd/programs", headers=admin).json()[0]
    tpl = client.post("/api/spd/path-templates", headers=admin, json={
        "program_id": program["id"], "code": "P197-T", "name": "版本号回归", "version": "v²"})
    assert tpl.status_code == 201, tpl.text
    copy = client.post(f"/api/spd/path-templates/{tpl.json()['id']}/copy", headers=admin, json={})
    assert copy.status_code == 201 and copy.json()["version"] == "v²-r2", (copy.status_code, copy.text[:200])
