"""患者分组的自动分组规则不经校验：写坏的规则存得进去，按规则批量入组即 500（P1-123）。

四类事实规则（病种纳入 / 排除、转诊触发、路径进入 / 完成、分组自动规则）里只有分组这一处建档时不走
`validate_conditions`。界面上「介于」只填一个数（填「60」）交上去的是 `[60]`：修前建分组 201，点「按规则批量
入组」求值时取上限 `IndexError`，500；分组没有改档接口，这条规则存进去就改不了，这个分组永远用不了按规则入组。

修法：建分组时查规则结构（422，存的仍是请求原样、出参字节不变）；修前存进去的坏规则，批量入组时 422 说清楚。
"""
import pytest

B = "/api/spd"


@pytest.mark.parametrize(("rule", "detail"), [
    ([{"field": "age", "op": "between", "value": [60]}], "自动分组规则非法：between 的 value 必须是 [下限, 上限]"),
    ([{"field": "age", "op": "between", "value": 60}], "自动分组规则非法：between 的 value 必须是 [下限, 上限]"),
    ([{"field": "age", "op": "约等于", "value": 60}], "自动分组规则非法：未知比较符：约等于"),
    ([{"op": ">=", "value": 60}], "自动分组规则非法：条件缺少 field"),
], ids=["介于只填一个数", "介于填成标量", "比较符写错", "缺字段"])
def test_建分组_写坏的自动规则一律422(client, admin, rule, detail):
    """修前四种写法都 201（条件不是对象的，请求体校验本就 422，不在此列）。"""
    resp = client.post(f"{B}/groups", headers=admin, json={"name": "P123 坏规则", "auto_rule": rule})
    assert resp.status_code == 422 and resp.json() == {"detail": detail}, resp.text[:300]


def test_合法规则照收_存的是请求原样(client, admin):
    rule = [{"field": "age", "op": "between", "value": [0, 150]}]
    created = client.post(f"{B}/groups", headers=admin, json={"name": "P123 好规则", "auto_rule": rule})
    assert created.status_code == 201, created.text
    row = next(g for g in client.get(f"{B}/groups", headers=admin).json() if g["id"] == created.json()["id"])
    assert row["auto_rule"] == rule   # 只查不改写：没有多出 label 之类的规范化键


def test_存量写坏的分组_按规则入组422说清楚_不再500(client, admin):
    """修前落库的坏规则：批量入组时说清楚、指路新建，不 500（修前 IndexError）。"""
    from app.database import SessionLocal
    from app.models import User
    from app.spd.models import SpdGroup

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P123 分组院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P123 患者", "id_card": "330106197001011680", "gender": "男", "birth_date": "1970-01-01"}).json()["id"]
    r = client.post(f"{B}/enrollments", headers=admin,
                    json={"patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert r.status_code == 201, r.text
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        legacy = SpdGroup(name="P123 存量坏规则", owner_user_id=admin_id, org_id=None,
                          auto_rule=[{"field": "age", "op": "between", "value": [60]}])
        db.add(legacy)
        db.commit()
        group_id = legacy.id
    resp = client.post(f"{B}/groups/{group_id}/members", headers=admin,
                       json={"use_auto_rule": True, "program_code": "hypertension"})
    assert resp.status_code == 422, resp.text[:300]
    assert resp.json() == {"detail": "该分组的自动分组规则写坏了（between 的 value 必须是 [下限, 上限]），"
                                     "无法按规则入组，请新建分组重写规则"}
    # 手工挑人入组不看规则，照常可用
    resp = client.post(f"{B}/groups/{group_id}/members", headers=admin, json={"patient_ids": [patient]})
    assert resp.status_code == 200 and resp.json() == {"added": 1, "total": 1}, resp.text
