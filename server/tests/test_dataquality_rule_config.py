"""数据质控规则的配置写坏一条，整个质控看板（汇总 / 运行检查）打不开（P2-81）。

规则的 `config` 随类型而异（区间的上下限、枚举的取值、引用的表与字段、逻辑校验名……），建 / 改规则原先只查
被检表登记与类型枚举，配置照单全收。「汇总」「运行检查」逐条扫全部启用规则，**任何一条**配置坏了整次扫描
500 / 422，看板打不开。字段名写错的不报错却悄悄失效（起止列不在，`datetime_order` 一条也不判）。界面只能
启停与改严重度，写得进去的是接口调用方（管理员）。

修法：`rule_config_problem` 按类型查配置，建 / 改规则 422；扫描时修前存进去的坏规则跳过，在出参
`skipped_rules`（只加字段）里点名，其余规则照常扫，页面上给出提示。
"""
import pytest

Q = "/api/dataquality"
BAD = [
    ("range", {"field": "name", "min": 5}, "name 是文字列，区间的 min 也要写成文字"),
    ("range", {"field": "created_at", "min": "2026-01-01"}, "created_at 不是数值或文字列，不能按区间判定"),
    ("enum", {"field": "gender", "values": 5}, "values 要写成取值列表"),
    ("cross_ref", {"field": "gender", "ref_table": "patients", "ref_field": "nope"}, "ref_field（nope）不是 patients 的字段"),
    ("cross_ref", {"field": "gender", "ref_table": "nope"}, "引用表 nope 未登记"),
    ("logic", {"check": "nope"}, None),
    ("required", {"field": "name", "filter": ["x"]}, "filter 要写成 {字段: 取值} 这样的对象"),
    ("required", {"field": "nme"}, "field（nme）不是 patients 的字段"),
    ("required", {"field": "name", "filter": {"statuss": "active"}}, "filter 里的 statuss 不是 patients 的字段"),
    ("logic", {"check": "datetime_order", "start_field": "created_at", "end_field": "ended"},
     "起止字段 ended 不是 patients 的字段"),
    ("cross_ref", {"field": "gender", "ref_code_system": "sex", "skip_empty": "yes"}, "skip_empty 只能是 true / false"),
]
IDS = ["区间界与文字列不符", "时间列按区间", "枚举取值不是列表", "引用列不存在", "引用表未登记", "逻辑校验未实现",
       "filter不是对象", "字段名写错", "filter字段写错", "起止字段写错", "开关是字符串"]


@pytest.mark.parametrize(("rule_type", "config", "detail"), BAD, ids=IDS)
def test_建规则_配置写坏一律422(client, admin, rule_type, config, detail):
    """修前这些写法建规则全部 201；前七种任一条都让汇总 / 运行检查整体 500 或 422，后四种悄悄失效或判错。"""
    resp = client.post(f"{Q}/rules", headers=admin, json={
        "code": "P281_BAD", "name": "P281 坏规则", "target_table": "patients", "rule_type": rule_type, "config": config})
    assert resp.status_code == 422, resp.text[:300]
    expected = detail or ("逻辑校验 nope 未实现（可选：chronic_followup_indicator、critical_closed_loop、"
                          "date_not_future、datetime_order、id_card_checksum）")
    assert resp.json() == {"detail": f"规则配置非法：{expected}"}


def test_改规则配置也查(client, admin):
    created = client.post(f"{Q}/rules", headers=admin, json={
        "code": "P281_EDIT", "name": "P281 改档", "target_table": "patients", "rule_type": "enum",
        "config": {"field": "gender", "values": ["男", "女"]}, "active": False})
    assert created.status_code == 201, created.text
    url = f"{Q}/rules/{created.json()['id']}"
    resp = client.patch(url, headers=admin, json={"config": {"field": "gender", "values": "男/女"}})
    assert resp.status_code == 422 and resp.json() == {"detail": "规则配置非法：values 要写成取值列表"}
    resp = client.patch(url, headers=admin, json={"severity": "warn"})   # 不动配置的改档不查
    assert resp.status_code == 200, resp.text


def test_存量坏规则_汇总与运行检查跳过并点名_其余照常扫(client, admin):
    """修前落库的坏规则：修前整次扫描 500；现在只有它被跳过并点名，种子规则照常扫。"""
    from app.database import SessionLocal
    from app.models import QcRule

    with SessionLocal() as db:
        broken = QcRule(code="P281_LEGACY", name="P281 存量坏规则", target_table="patients", rule_type="range",
                        config={"field": "name", "min": 5}, severity="error", active=True)
        db.add(broken)
        db.commit()
        broken_id = broken.id
    try:
        expected = [{"rule_code": "P281_LEGACY", "rule_name": "P281 存量坏规则",
                     "problem": "name 是文字列，区间的 min 也要写成文字"}]
        summary = client.get(f"{Q}/summary", headers=admin)
        assert summary.status_code == 200, summary.text[:300]
        assert summary.json()["skipped_rules"] == expected
        assert "P281_LEGACY" not in [r["rule_code"] for r in summary.json()["by_rule"]]
        assert summary.json()["rules_checked"] >= 15   # 种子规则一条没少扫
        run = client.get(f"{Q}/run", headers=admin)
        assert run.status_code == 200 and run.json()["skipped_rules"] == expected, run.text[:300]
    finally:
        with SessionLocal() as db:
            db.query(QcRule).filter(QcRule.id == broken_id).delete()
            db.commit()


def test_种子规则都过得了同一道校验():
    from app.data.qc_rules_seed import SEED_QC_RULES
    from app.routers.dataquality import rule_config_problem

    assert SEED_QC_RULES
    assert [r["code"] for r in SEED_QC_RULES
            if rule_config_problem(r["target_table"], r["rule_type"], r["config"])] == []
