"""数据质控规则 filter 的取值写成列表、枚举取值多套一层方括号：建规则 201，之后汇总与运行检查整体 500（P2-1123，
第三十二批扫描 B4-4；P2-81 没覆盖的两种形状）。

修前（b3069f0 实测）：`rule_config_problem` 对 filter 只查是对象、键是字段，对枚举只查 values 是列表——
`{"filter": {"encounter_type": ["outpatient", "inpatient"]}}`（想表达「属于这几种」）绑定参数不收列表，
`{"values": [["男", "女"]]}` 放不进集合，两者建规则都 201，之后 summary 500、run 500。质控页载入即取汇总，
页打不开，页上的停用按钮也就点不到，只能直接调接口停用。

修后：filter 的取值与枚举元素只收单个值，建 / 改规则 422；存量里的这两种扫描时跳过并在 `skipped_rules` 点名；
扫描时仍抛错的规则（校验还没认出来的写法）逐条兜住，同样跳过并写明异常类，其余规则照常扫（口径同 P2-81）。
"""
from app.database import SessionLocal
from app.models import QcRule
from app.routers import dataquality

Q = "/api/dataquality"
FILTER_LIST = {"field": "diagnosis_code", "filter": {"encounter_type": ["outpatient", "inpatient"]}}
ENUM_NESTED = {"field": "gender", "values": [["男", "女"]]}
FILTER_PROBLEM = "filter 里 encounter_type 的取值要写成单个值（按「字段 = 取值」过滤，不支持列表或对象）"
ENUM_PROBLEM = "values 里每个取值都要写成单个值（文字或数），不能再套一层列表或对象"


def _create(client, admin, code, table, rule_type, config):
    """建成停用的：修法撤掉时建得进去的坏规则不参与扫描，不连累后面两条用例。"""
    return client.post(f"{Q}/rules", headers=admin, json={
        "code": code, "name": f"{code} 规则", "target_table": table, "rule_type": rule_type, "config": config,
        "severity": "warn", "active": False})


def _stock(*rules):
    """修前存进去的规则：绕过接口直接落库，返回编号（用完删掉，不串到别的用例）。"""
    with SessionLocal() as db:
        rows = [QcRule(code=code, name=f"{code} 规则", target_table=table, rule_type=rule_type, config=config,
                       severity="warn", active=True) for code, table, rule_type, config in rules]
        db.add_all(rows)
        db.commit()
        return [row.id for row in rows]


def _drop(ids):
    with SessionLocal() as db:
        db.query(QcRule).filter(QcRule.id.in_(ids)).delete(synchronize_session=False)
        db.commit()


def test_filter取值写成列表_枚举多套一层方括号_建与改都422(client, admin):
    got = _create(client, admin, "P21123_FLIST", "encounters", "required", FILTER_LIST)
    assert got.status_code == 422 and got.json() == {"detail": f"规则配置非法：{FILTER_PROBLEM}"}, got.text   # 修前 201
    got = _create(client, admin, "P21123_ENEST", "patients", "enum", ENUM_NESTED)
    assert got.status_code == 422 and got.json() == {"detail": f"规则配置非法：{ENUM_PROBLEM}"}, got.text   # 修前 201

    # 改规则同一句；单个取值（含 null：按「列 IS NULL」过滤）照收
    flt = _create(client, admin, "P21123_FONE", "encounters", "required",
                  {"field": "diagnosis_code", "filter": {"encounter_type": "outpatient"}})
    enum = _create(client, admin, "P21123_EONE", "patients", "enum", {"field": "gender", "values": ["男", "女"]})
    assert (flt.status_code, enum.status_code) == (201, 201), (flt.text, enum.text)
    got = client.patch(f"{Q}/rules/{flt.json()['id']}", headers=admin, json={"config": FILTER_LIST})
    assert got.status_code == 422 and got.json() == {"detail": f"规则配置非法：{FILTER_PROBLEM}"}, got.text   # 修前 200
    got = client.patch(f"{Q}/rules/{enum.json()['id']}", headers=admin, json={"config": ENUM_NESTED})
    assert got.status_code == 422 and got.json() == {"detail": f"规则配置非法：{ENUM_PROBLEM}"}, got.text   # 修前 200
    got = client.patch(f"{Q}/rules/{flt.json()['id']}", headers=admin,
                       json={"config": {"field": "diagnosis_code", "filter": {"encounter_type": None}}})
    assert got.status_code == 200, got.text


def test_存量里的这两种_汇总与运行检查仍200_跳过并点名(client, admin):
    ids = _stock(("P21123_OLD_FLIST", "encounters", "required", FILTER_LIST),
                 ("P21123_OLD_ENEST", "patients", "enum", ENUM_NESTED))
    try:
        expected = [{"rule_code": "P21123_OLD_ENEST", "rule_name": "P21123_OLD_ENEST 规则", "problem": ENUM_PROBLEM},
                    {"rule_code": "P21123_OLD_FLIST", "rule_name": "P21123_OLD_FLIST 规则", "problem": FILTER_PROBLEM}]
        summary = client.get(f"{Q}/summary", headers=admin)
        assert summary.status_code == 200, summary.text[:300]   # 修前 500：质控页打不开
        assert summary.json()["skipped_rules"] == expected and summary.json()["rules_checked"] >= 15
        run = client.get(f"{Q}/run", headers=admin)
        assert run.status_code == 200 and run.json()["skipped_rules"] == expected, run.text[:300]   # 修前 500
    finally:
        _drop(ids)


def test_扫描时仍抛错的规则_逐条兜住_写明异常类_其余照扫(client, admin, monkeypatch):
    """兜底：配置校验还没认出来的写法（真 PG 上列与取值的类型对不上之类）扫描时抛错，只跳过这一条。"""
    ids = _stock(("P21123_BOOM", "patients", "required", {"field": "name"}))
    real = dataquality.run_rule

    def flaky(db, rule):
        if rule.code == "P21123_BOOM":
            raise RuntimeError("扫描里的意外")
        return real(db, rule)

    monkeypatch.setattr(dataquality, "run_rule", flaky)
    try:
        expected = [{"rule_code": "P21123_BOOM", "rule_name": "P21123_BOOM 规则",
                     "problem": "扫描时出错（RuntimeError），这次没扫，请核对配置"}]
        summary = client.get(f"{Q}/summary", headers=admin)
        assert summary.status_code == 200, summary.text[:300]   # 不兜底即 500
        body = summary.json()
        assert body["skipped_rules"] == expected and "P21123_BOOM" not in [r["rule_code"] for r in body["by_rule"]]
        assert body["rules_checked"] >= 15   # 种子规则一条没少扫
        run = client.get(f"{Q}/run", headers=admin)
        assert run.status_code == 200 and run.json()["skipped_rules"] == expected, run.text[:300]
    finally:
        _drop(ids)
