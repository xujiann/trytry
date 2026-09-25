"""动词级孤儿端点棘轮：路径接上了界面，不等于这个动词有入口（P2-93）。

`test_orphan_endpoints` 按**路径**算——「同一路径多个动词只算一条，界面调的是路径，动词由后端用例守」。可后端用例守的
是接口对不对，不是界面上有没有入口：P2-92 的随访方案，清单 `GET /followup-rules` 有页面在调，这条路径就算接上了；
建方案的 `POST` 一个入口都没有，方案面板只能编辑、编辑框里又没有关键词——出院即派生随访与「按患者特征自动匹配」从
界面上一个人都匹配不到，那道棘轮一直是绿的。

2026-09-25 按「写接口的（动词, 路径）在前端有没有同动词的调用」量全平台：路径有入口、这个动词没有的 21 条。大半是
配置项**只能改、不能建**（评估量表、宣教素材、考核指标、数据源、报告模板、DRG 分组、数据质控规则、中医适宜技术、
门诊知情同意模板……）——正是需求对照表「全部可配置，标准或指标调整时通过管理端配置即可完成」的那一类；还有窗口
代录（知情同意、更正申请）、手工建慢专病任务、几处删除。

## 判据

- **分母**：写接口（POST / PUT / PATCH / DELETE）里，路径已被 `test_orphan_endpoints` 算作有入口的。路径一条调用
  都没有的归那道棘轮管，这里不重复算。
- **有没有入口**：复用 `test_frontend_api_calls_resolve.scan()` 的调用点解析（地址字面量 + 按接地址的函数推动词：
  `api` / `authApi` / `fetch` 看 `method`、`postAction` 看第四个实参、医生端 `spdPost` / `act` 恒 POST），取它
  推得出动词的那些调用对上的（动词, 路由）。**不另写一套解析**：那道闸门零基线守着「地址对得上路由、动词后端都接」，
  两边共用一把尺子。
- 推不出动词的调用点（选项对象是变量等）不算入口——宁可多点名一条让人看一眼，不替它假定动词。

## 名单

- `EXEMPT`：按设计没有界面的（对机器不对人），写明理由，只许变少。
- `KNOWN`：该有入口还没有的，只许变少；接上一条就划掉一条（不划掉也红，与孤儿棘轮同一个双向钉法）。
"""
from __future__ import annotations

import test_api_contract_governance as contract
import test_frontend_api_calls_resolve as resolve
import test_orphan_endpoints as orphan

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

#: 按设计没有界面的写动词。只许变少；新增须写明**为什么这个动作不需要界面**。
EXEMPT: dict[str, str] = {
    "POST /api/esb/messages": (
        "ESB 接入方的消息入队：按 X-Esb-Endpoint + X-Esb-Token 鉴权（不收平台账号令牌），由外部系统调用；"
        "管理端的消息页是查看与重处理，不替接入方发消息"
    ),
}

#: 欠账名单：路径有入口、这个动词还没有的。只许变少，按模块分组。
KNOWN: set[str] = {
    # 配置项只能改、不能建（需求对照表 7.5「全部可配置」）
    "POST /api/accounting/subjects",            # 会计科目
    "POST /api/chronic/disease-types",          # 慢病病种目录（页面只有编辑）
    "POST /api/dataquality/rules",              # 数据质控规则
    "POST /api/drgs/groups",                    # DRG 分组（页面只调权重）
    "POST /api/outpatient/consent-templates",   # 门诊知情同意模板
    "POST /api/tcm/techniques",                 # 中医适宜技术目录
    "POST /api/tcm-heritage/simulations",       # 模拟诊疗病例（情境化决策）
    "POST /api/spd/indicators",                 # 考核指标
    "POST /api/spd/report-templates",           # 报告模板
    "POST /api/spd/scales",                     # 评估量表（页面只有发布 / 停用）
    "PATCH /api/spd/scales/{scale_id}",
    "PATCH /api/rbac/roles/{role_id}",          # 角色改名 / 说明
    # 窗口代录（docstring：患者到柜台口头 / 书面提出，经办人代录）
    "POST /api/consents",
    "POST /api/consents/corrections",
    # 删除只有接口（页面有启停，没有删）
    "DELETE /api/dataquality/rules/{rule_id}",
}


def _write_endpoints() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for _module, route in contract._iter_endpoints()
        if route.path.startswith("/api/")
        for method in (route.methods or ())
        if method in WRITE_METHODS
    }


def verb_orphans(files: dict[str, str] | None = None) -> set[str]:
    """`动词 路径`——路径有前端入口、这个写动词没有的。`files` 给自证用（文件名 → 源码）。"""
    corpus = None if files is None else "\n".join(files.values())
    wired = set(orphan.endpoint_paths()) - orphan.orphan_paths(corpus)
    called = set(resolve.scan(files)["calls"])
    return {f"{method} {path}" for method, path in _write_endpoints()
            if path in wired and (method, path) not in called}


def test_没有新的动词级孤儿():
    fresh = sorted(verb_orphans() - KNOWN - set(EXEMPT))
    assert not fresh, (
        "以下写动词在前端没有入口（同一路径的别的动词有），且不在欠账名单里——新动词要么带界面来，"
        "要么按设计不需要界面就进 EXEMPT 并写明理由：\n  " + "\n  ".join(fresh)
    )


def test_登记的仍然没有入口_接上了就划掉():
    found = verb_orphans()
    wired = sorted(KNOWN - found)
    assert not wired, (
        "这些动词已经有前端入口了（或端点已不存在）——从 KNOWN 里划掉，欠账要看得见它在变少：\n  "
        + "\n  ".join(wired)
    )
    stale = sorted(set(EXEMPT) - found)
    assert not stale, f"豁免的动词已有前端入口或端点已不存在，划掉：{stale}"
    assert not KNOWN & set(EXEMPT), "既豁免又欠账"
    assert all(why.strip() for why in EXEMPT.values())
    assert len(EXEMPT) <= 1, "豁免只许变少：新增前先问这个动作是不是真的没有人会在页面上做"


def test_判据自证_只调了清单的建接口点名_同动词调过的放过():
    """P2-92 修之前的随访方案页就长这样：清单 GET、改档 PATCH，建方案的 POST 没有入口。"""
    src = "\n".join([
        'await api("/api/spd/followup-rules");',
        'return postAction(`/api/spd/followup-rules/${id}`, body, "#m", "PATCH");',
        'await api(`/api/spd/scales/${id}`);',
        'return postAction(`/api/staffing/secondments/${id}/end${q}`, null, "#m");',
        'act("/api/spd/point-accounts/signin", null, (r) => r.points);',
        'await authApi(`/api/portal/me/family/${m}`, { method: "DELETE" });',
        'await api(url, { method: "POST" });   // 地址不是字面量：看不到，不算任何路径的入口',
    ])
    found = verb_orphans({"自证.js": src})
    assert "POST /api/spd/followup-rules" in found            # 只调过清单
    assert "PATCH /api/spd/scales/{scale_id}" in found        # 只读过详情
    for called in ("PATCH /api/spd/followup-rules/{rule_id}", "POST /api/staffing/secondments/{secondment_id}/end",
                   "POST /api/spd/point-accounts/signin", "DELETE /api/portal/me/family/{member_id}"):
        assert called not in found, called
    # 路径一条调用都没有的不在分母里（归孤儿棘轮）
    assert not any(v.endswith(" /api/spd/tasks") for v in found)


def test_分母与调用点都没有空转():
    writes = _write_endpoints()
    assert len(writes) >= 400, f"只枚举到 {len(writes)} 个写接口，分母可疑"
    calls = resolve.scan()["calls"]
    writes_called = {c for c in calls if c[0] in WRITE_METHODS}
    assert len(writes_called) >= 300, f"只认出 {len(writes_called)} 个写接口有入口，调用点解析可能失灵"
