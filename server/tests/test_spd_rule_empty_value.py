"""慢专病规则条件的值框留空照样存：一行空条件就让纳入规则谁都纳不进、排除 / 转诊 / 问卷异常规则人人命中（P2-1117，
第三十二批「规则与配置的求值口径」扫描 B4-1）。

`validate_conditions` 在配置时只查大小比较与「介于」的比较值（P2-712），等于 / 不等于 / 包含的值留空、属于 / 不属于
的列表为空照存——页面值框留空交上来的就是 `""`，属于 / 不属于交 `[]`，新加一行的缺省是「年龄 等于（空）」。求值时
`== ""`、`in []` 永远不成立，`!= ""`、`contains ""`、`not_in []` 对任何有值的事实都成立。修前实测（b4/r7*.py）：
高血压排除规则加一行「诊断名称 包含（空）」PATCH 200 并静默升版，成人 I10 登记筛查从 suspect 变 excluded；纳入规则
多一行未动过的缺省行，成人 I10 登记筛查变 normal、批量识别 suspect 0；转诊规则「诊断 不属于（空）」201，上感患者
试算 triggered；问卷异常规则「症状 不等于（空）」判重度 201，作答「无」得 high、派「立即上转」。

修法：`_check_comparison_value` 同 P2-466 / P2-712「永不命中、也不报错的比较值在配置时拦」的口径补两条——等于 /
不等于 / 包含的值去掉空白后不得为空；属于 / 不属于必须是非空列表、元素去掉空白后不得为空。五个写入口（病种建档 /
改规则、转诊规则、分组自动规则、问卷异常规则、路径节点条件）共用这一道，各钉一条 422。存量不改。
"""
import pytest

from app.spd.rules import RuleError, validate_conditions

B = "/api/spd"


@pytest.mark.parametrize(("cond", "detail"), [
    ({"field": "age", "op": "==", "value": ""}, "条件 age 的比较值不能为空（「等于」空值永远不会命中）"),
    ({"field": "diagnosis_name", "op": "!=", "value": "  "},
     "条件 diagnosis_name 的比较值不能为空（「不等于」空值对任何有值的数据都成立）"),
    ({"field": "diagnosis_name", "op": "contains", "value": ""},
     "条件 diagnosis_name 的比较值不能为空（「包含」空值对任何有值的数据都成立）"),
    ({"field": "gender", "op": "=="}, "条件 gender 的比较值不能为空（「等于」空值永远不会命中）"),
    ({"field": "diagnosis", "op": "in", "value": []},
     "条件 diagnosis 的「属于」至少要填一个值（空列表永远不会命中）"),
    ({"field": "diagnosis", "op": "not_in", "value": []},
     "条件 diagnosis 的「不属于」至少要填一个值（空列表对任何有值的数据都成立）"),
    ({"field": "diagnosis", "op": "in", "value": ""},
     "条件 diagnosis 的「属于」要写成值的列表（收到 ''）"),
    ({"field": "diagnosis", "op": "not_in", "value": ["I10", " "]},
     "条件 diagnosis 的「不属于」列表里有空值（收到 ['I10', ' ']）"),
], ids=["等于空串", "不等于空白", "包含空串", "等于没写值", "属于空列表", "不属于空列表", "属于空串", "不属于含空元素"])
def test_留空的比较值_配置时报错(cond, detail):
    """修前八种写法都校验通过、照存。"""
    with pytest.raises(RuleError) as exc:
        validate_conditions([cond])
    assert str(exc.value) == detail


@pytest.mark.parametrize("cond", [
    {"field": "age", "op": "==", "value": 0},
    {"field": "symptom", "op": "!=", "value": "无"},
    {"field": "diagnosis_name", "op": "contains", "value": "高血压"},
    {"field": "age", "op": "in", "value": [60, 65]},
    {"field": "diagnosis", "op": "not_in", "value": ["E10"]},
    {"field": "ua", "op": "exists", "value": True},
    {"field": "ua", "op": "exists"},
], ids=["等于0", "不等于文本", "包含文本", "属于数列表", "不属于文本列表", "存在", "存在不写值"])
def test_填了值的照收(cond):
    assert validate_conditions([cond])[0]["value"] == cond.get("value")


@pytest.mark.parametrize("cond", [
    {"field": "diagnosis_name", "op": "contains", "value": "\u200b"},
    {"field": "gender", "op": "==", "value": "\ufeff"},
    {"field": "diagnosis_name", "op": "!=", "value": " \u2060\x01 "},
    {"field": "diagnosis", "op": "in", "value": ["I10", "\u200b"]},
], ids=["包含零宽空格", "等于BOM", "不等于空白夹词连接符与控制字符", "属于含零宽元素"])
def test_只有看不见的字符的比较值_与留空同样报错(cond):
    """P2-1148 跟进：只有零宽空格、BOM、控制字符的比较值原先按 `strip()` 判不算空、照存——「包含」两侧过 `text_key`
    后它归一成空、按原样比永不命中（P2-1147），「等于」同样永不命中，与留空是同一个坏配置。判空改用必填文本同一个判据
    `texttypes.is_blank_text`；夹着零宽字符、但有看得见的字的照收（见下一条）。"""
    with pytest.raises(RuleError):
        validate_conditions([cond])   # 修前照收


def test_夹着零宽字符但有字的比较值_照收():
    (cond,) = validate_conditions([{"field": "diagnosis_name", "op": "contains", "value": "高\u200b血压"}])
    assert cond["value"] == "高\u200b血压"   # 存进去的字节不变，求值时两侧过 text_key


def test_病种建档与改规则_空条件422_不升版(client, admin):
    """修前：建病种 201；改高血压排除规则 PATCH 200、v1 → v2，成人 I10 登记筛查从 suspect 变 excluded。"""
    created = client.post(f"{B}/programs", headers=admin, json={
        "code": "p1117_prog", "name": "P1117 病种", "category": "chronic",
        "include_rules": [{"field": "diagnosis", "op": "in", "value": ["I10"]},
                          {"field": "age", "op": "==", "value": ""}]})
    assert created.status_code == 422, created.text[:300]
    assert created.json() == {"detail": "条件 age 的比较值不能为空（「等于」空值永远不会命中）"}

    ht = next(p for p in client.get(f"{B}/programs", headers=admin).json() if p["code"] == "hypertension")
    patched = client.patch(f"{B}/programs/{ht['id']}", headers=admin, json={
        "exclude_rules": ht["exclude_rules"] + [{"field": "diagnosis_name", "op": "contains", "value": ""}],
        "note": "P1117"})
    assert patched.status_code == 422, patched.text[:300]
    after = next(p for p in client.get(f"{B}/programs", headers=admin).json() if p["code"] == "hypertension")
    assert after["version"] == ht["version"] and after["exclude_rules"] == ht["exclude_rules"]


def test_转诊规则_空条件422(client, admin):
    """修前 201：「诊断 不属于（空）」让一名上感患者试算 triggered。改规则同一道。"""
    resp = client.post(f"{B}/referral-rules", headers=admin, json={
        "code": "P1117_RR", "name": "P1117 空值规则",
        "conditions": [{"field": "diagnosis", "op": "not_in", "value": []}]})
    assert resp.status_code == 422, resp.text[:300]
    assert resp.json() == {"detail": "条件 diagnosis 的「不属于」至少要填一个值（空列表对任何有值的数据都成立）"}

    ok = client.post(f"{B}/referral-rules", headers=admin, json={
        "code": "P1117_RR_OK", "name": "P1117 好规则", "conditions": [{"field": "bp_sys", "op": ">=", "value": 180}]})
    assert ok.status_code == 201, ok.text[:300]
    patched = client.patch(f"{B}/referral-rules/{ok.json()['id']}", headers=admin, json={
        "conditions": [{"field": "diagnosis_name", "op": "!=", "value": ""}]})
    assert patched.status_code == 422, patched.text[:300]


def test_分组自动规则_空条件422(client, admin):
    resp = client.post(f"{B}/groups", headers=admin, json={
        "name": "P1117 分组", "auto_rule": [{"field": "risk_level", "op": "!=", "value": " "}]})
    assert resp.status_code == 422, resp.text[:300]   # 修前 201，按规则批量入组把在管的人全拉进来
    assert resp.json() == {"detail": "自动分组规则非法：条件 risk_level 的比较值不能为空（「不等于」空值对任何有值的数据都成立）"}


def test_问卷异常规则_空条件422(client, admin):
    """修前 201：作答「无」判重度、派「立即上转」处置任务。"""
    resp = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": "P1117_Q", "name": "P1117 问卷", "scene": "outpatient",
        "items": [{"key": "symptom", "title": "近期症状", "type": "single",
                   "options": [{"label": "无"}, {"label": "头晕"}]}],
        "abnormal_rules": [{"when": {"field": "symptom", "op": "!=", "value": ""}, "level": "high",
                            "action": "立即上转"}]})
    assert resp.status_code == 422, resp.text[:300]
    assert resp.json() == {"detail": "异常分级规则非法：条件 symptom 的比较值不能为空（「不等于」空值对任何有值的数据都成立）"}


def test_路径节点条件_空条件422(client, admin):
    """只能走接口的路径节点进入 / 完成条件，加节点与改节点同一道。"""
    ht = next(p for p in client.get(f"{B}/programs", headers=admin).json() if p["code"] == "hypertension")
    template = client.post(f"{B}/path-templates", headers=admin, json={
        "program_id": ht["id"], "code": "P1117-PATH", "name": "P1117 路径"})
    assert template.status_code == 201, template.text[:300]
    tid = template.json()["id"]
    bad = client.post(f"{B}/path-templates/{tid}/nodes", headers=admin, json={
        "key": "p1117_n1", "name": "风险复评", "enter_condition": [{"field": "risk_level", "op": "==", "value": ""}]})
    assert bad.status_code == 422, bad.text[:300]   # 修前 201，进入条件永远不满足、路径走到这里就暂停
    assert bad.json() == {"detail": "条件 risk_level 的比较值不能为空（「等于」空值永远不会命中）"}

    node = client.post(f"{B}/path-templates/{tid}/nodes", headers=admin, json={"key": "p1117_n1", "name": "风险复评"})
    assert node.status_code == 201, node.text[:300]
    patched = client.patch(f"{B}/path-nodes/{node.json()['id']}", headers=admin, json={
        "complete_condition": [{"field": "diagnosis", "op": "in", "value": []}]})
    assert patched.status_code == 422, patched.text[:300]
    assert patched.json() == {"detail": "条件 diagnosis 的「属于」至少要填一个值（空列表永远不会命中）"}
