"""阶段十遗留项：流程图形化编排与财务合并报表。

这两项写在第七轮计划的阶段十与阶段九·五第三批里，第一轮执行时被跳过了。
补上并配用例。
"""

from pathlib import Path

import pytest


# ================================================================ 图形化编排


def test_画布产出的定义与手写走同一个接口(client, admin):
    """画布只是录入方式的差别，不是另一套模型。

    若画布产出的结构与手写的不一样，就等于凭空多出一个只有画布能生成、
    其它入口都不认的流程格式。
    """
    js = client.get("/static/pages-mgmt.js").text
    assert "wfCanvasInit" in js, "画布逻辑没有加载"
    # 画布保存走的必须是既有接口，后端一行没改
    assert 'postAction("/api/workflows/definitions", { key, name, nodes: WF_NODES }' in js
    # 不允许出现画布专用的新接口
    assert "/api/workflows/canvas" not in js and "/api/workflows/graph" not in js


def test_画布的校验与服务端三条规则一致(client, admin):
    """键唯一 / next 指向存在 / 至少一个终态——三条都要在前端也拦一次，
    因为这三种错留到运行期的表现是单据卡死在某个节点上没人能推。"""
    js = client.get("/static/pages-mgmt.js").text
    body = js[js.index("function wfValidate"):js.index("function wfCanvasDraw")]
    assert "节点编码不得重复" in body
    assert "指向不存在的节点" in body
    assert "必须有终态节点" in body

    # 锚在 __file__ 而非 CWD——隔离工作目录跑单测（多 agent 并行的既定做法）不该假红
    workflows_py = Path(__file__).resolve().parent.parent / "app" / "routers" / "workflows.py"
    server = workflows_py.read_text(encoding="utf-8")
    rules = server[server.index("def _validate_nodes"):server.index("def _validate_nodes") + 1200]
    assert "节点 key 不得重复" in rules
    assert "指向不存在的节点" in rules
    assert "必须有终态节点" in rules


def test_画布产出的定义能被后端接受(client, admin):
    """把画布会生成的那种结构真的提交一次——前端断言再多，
    也不如让后端收一次来得可信。"""
    nodes = [
        {"key": "apply", "name": "申请", "role": "doctor", "next": "approve"},
        {"key": "approve", "name": "审批", "role": "director", "next": ""},
    ]
    resp = client.post(
        "/api/workflows/definitions",
        json={"key": "canvas_demo", "name": "画布产出的流程", "nodes": nodes},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert [n["key"] for n in resp.json()["nodes"]] == ["apply", "approve"]


@pytest.mark.parametrize(
    "nodes,reason",
    [
        ([{"key": "a", "name": "甲", "next": "a2"}], "next 指向不存在"),
        ([{"key": "a", "name": "甲", "next": "b"}, {"key": "a", "name": "乙", "next": ""}], "键重复"),
        ([{"key": "a", "name": "甲", "next": "b"}, {"key": "b", "name": "乙", "next": "a"}], "没有终态"),
    ],
)
def test_三种非法定义后端都拦得住(client, admin, nodes, reason):
    resp = client.post(
        "/api/workflows/definitions",
        json={"key": f"bad_{abs(hash(reason)) % 9999}", "name": "非法流程", "nodes": nodes},
        headers=admin,
    )
    assert resp.status_code == 422, f"{reason} 未被拦下：{resp.text}"


# ================================================================ 合并报表


@pytest.fixture(scope="module")
def two_orgs_with_vouchers(client, admin):
    """两家机构各记一笔已过账凭证，用来验合并。"""
    orgs = [
        client.post(
            "/api/organizations",
            json={"name": name, "org_type": t, "level": lv},
            headers=admin,
        ).json()
        for name, t, lv in [("合并总院", "lead_hospital", "county"),
                            ("合并卫生院", "township", "township")]
    ]
    period = "2026-07"
    # 总院：收入 10000（贷）/ 银行存款 10000（借）
    client.post(
        "/api/accounting/vouchers",
        json={"org_id": orgs[0]["id"], "period": period, "voucher_no": "JZ001",
              "voucher_date": "2026-07-15", "summary": "医疗收入",
              "entries": [{"subject_code": "1002", "debit": 10000, "credit": 0},
                          {"subject_code": "4001", "debit": 0, "credit": 10000}]},
        headers=admin,
    )
    # 卫生院：费用 4000（借）/ 银行存款 4000（贷）
    client.post(
        "/api/accounting/vouchers",
        json={"org_id": orgs[1]["id"], "period": period, "voucher_no": "JZ002",
              "voucher_date": "2026-07-20", "summary": "医疗支出",
              "entries": [{"subject_code": "5001", "debit": 4000, "credit": 0},
                          {"subject_code": "1002", "debit": 0, "credit": 4000}]},
        headers=admin,
    )
    for v in client.get("/api/accounting/vouchers", params={"period": period},
                        headers=admin).json():
        client.post(f"/api/accounting/vouchers/{v['id']}/post", headers=admin)
    return {"orgs": orgs, "period": period}


def test_合并报表按机构分列且给出合并列(client, admin, two_orgs_with_vouchers):
    """医共体"独立建账、集中核算"要的正是"每家多少、合起来多少"同时看得见。"""
    period = two_orgs_with_vouchers["period"]
    resp = client.get(
        "/api/accounting/consolidated-statements", params={"period": period}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["orgs"]) == 2
    lead = body["orgs"][0]
    assert lead["income_statement"]["income"] == 10000.0
    assert lead["income_statement"]["expense"] == 0.0
    township = body["orgs"][1]
    assert township["income_statement"]["expense"] == 4000.0
    # 合并列 = 各院相加
    assert body["consolidated"]["income_statement"]["income"] == 10000.0
    assert body["consolidated"]["income_statement"]["expense"] == 4000.0
    assert body["consolidated"]["income_statement"]["surplus"] == 6000.0


def test_负债与收入按贷减借不会变成负数(client, admin, two_orgs_with_vouchers):
    """统一用一个方向算，负债会变成负数，报表直接不能看。"""
    body = client.get(
        "/api/accounting/consolidated-statements",
        params={"period": two_orgs_with_vouchers["period"]}, headers=admin,
    ).json()
    assert body["consolidated"]["income_statement"]["income"] > 0
    assert "资产/费用按借−贷" in body["caliber"]["direction"]


def test_明说未做内部往来抵销(client, admin, two_orgs_with_vouchers):
    """平台没有内部交易标记，做不了抵销——如实称叠加汇总，
    不冒充合并会计报表。"""
    body = client.get(
        "/api/accounting/consolidated-statements",
        params={"period": two_orgs_with_vouchers["period"]}, headers=admin,
    ).json()
    assert body["caliber"]["elimination"] is False
    assert "未做内部往来抵销" in body["caliber"]["note"]


def test_合并报表支持按分组筛选(client, admin, two_orgs_with_vouchers):
    orgs = two_orgs_with_vouchers["orgs"]
    group = client.post(
        "/api/org-groups",
        json={"name": "合并测试片区", "group_type": "zone", "lead_org_id": orgs[0]["id"]},
        headers=admin,
    ).json()
    client.post(
        f"/api/org-groups/{group['id']}/members",
        json={"org_id": orgs[1]["id"]}, headers=admin,
    )
    body = client.get(
        "/api/accounting/consolidated-statements",
        params={"period": two_orgs_with_vouchers["period"], "group_id": group["id"]},
        headers=admin,
    ).json()
    assert [o["org_id"] for o in body["orgs"]] == [orgs[1]["id"]]
    assert body["consolidated"]["income_statement"]["expense"] == 4000.0


def test_未过账凭证不进合并报表(client, admin, two_orgs_with_vouchers):
    """草稿未生效、作废已撤销，计进去就不是账了——与试算平衡表同一口径。"""
    orgs = two_orgs_with_vouchers["orgs"]
    client.post(
        "/api/accounting/vouchers",
        json={"org_id": orgs[0]["id"], "period": two_orgs_with_vouchers["period"],
              "voucher_no": "JZ999", "voucher_date": "2026-07-28", "summary": "草稿不算数",
              "entries": [{"subject_code": "1002", "debit": 99999, "credit": 0},
                          {"subject_code": "4001", "debit": 0, "credit": 99999}]},
        headers=admin,
    )
    body = client.get(
        "/api/accounting/consolidated-statements",
        params={"period": two_orgs_with_vouchers["period"]}, headers=admin,
    ).json()
    assert body["consolidated"]["income_statement"]["income"] == 10000.0
