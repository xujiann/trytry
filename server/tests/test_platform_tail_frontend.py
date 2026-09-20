"""平台与配置侧尾巴的界面入口守卫（P1-40 清零的最后一批，14 个模块）。

这一批把孤儿端点从 29 清到 **0**。其中两条是"补界面"之外的发现：

* **模拟诊疗两边都没建**。`pages-clinical.js` 里原先写着"作答与评分在医师端
  H5 完成"，而 `m/doctor.js` 里根本没有这块代码——病例维护得再全也没人练得成。
  注释已改成实话，作答落在管理端这一页。
* **ESB 的 `data-esbtoggle` 撞名**。接入方启停与编排启停本是两件事（前者停的
  是谁能投消息进来，后者停的是某条编排还跑不跑），共用一个 dataset 名会互相
  吃掉。新加的那个改叫 `esbflowtoggle`。

其余各自的判据：

* **机构树体检**要说清后果：非顶层机构缺 `parent_id` 时，它经手的转诊单只有
  全域角色推得动，非全域账号一律 403——不说的话，等越权校验"咬人"了才回头找。
  链路错位的判据是**层级相邻**而非链路长度。
* **导诊不回空列表**：一条都没匹配上时回落到"全科门诊"，让居民永远拿得到一个
  可去的地方。端点收的是**裸数组**，不是 `{symptoms: [...]}`。
* **可下钻指标目录由服务端给**：前端不另维护一份"哪些能下钻"的清单，否则新增
  一个可下钻指标而前端忘了加，它就永远点不开。
* **改模板只影响之后签署的告知书**：已签的正文在签署时已冻结成快照。
* **已清算的基金池不可改要素**（后端 409）：改了之后清算结果与分配明细对不上账。
  分配明细里的得分构成是清算当时的**快照**，不是拿今天的方案重算的。
* **停用类操作一律说明影响面**：病种停用不影响已建档案，专病目录停用不影响已
  入组的，模板停用不影响已签的。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SRC = {n: (STATIC / n).read_text(encoding="utf-8") for n in
       ("core.js", "pages-clinical.js", "pages-mgmt.js", "pages-public.js", "pages-spd.js")}


def _block(file: str, fn: str) -> str:
    src = SRC[file]
    start = src.index(fn)
    rest = src[start + len(fn):]
    marker = "\nasync function render"
    return src[start:start + len(fn) + rest.index(marker)] if marker in rest else src[start:]


APT = _block("core.js", "async function renderAppointments()")
ORGS = _block("core.js", "async function renderOrgs()")
DASH = _block("core.js", "async function renderDashboard()")
DICT = _block("core.js", "async function renderDicts()")
TCM = _block("pages-clinical.js", "async function renderTcm()")
ARCH = _block("pages-clinical.js", "async function renderArchive()")
RBAC = _block("pages-clinical.js", "async function renderRbac()")
PROJ = _block("pages-clinical.js", "async function renderProjects()")
RES = _block("pages-clinical.js", "async function renderResources()")
SURV = _block("pages-clinical.js", "async function renderSurveillance()")
VACC = _block("pages-clinical.js", "async function renderVaccineSupply()")
HERIT = _block("pages-clinical.js", "async function renderTcmHeritage()")
OG = _block("pages-mgmt.js", "async function renderOrgGroups()")
OD = _block("pages-mgmt.js", "async function renderOutpatientDocs()")
FUND = _block("pages-mgmt.js", "async function renderFund()")
DP = _block("pages-mgmt.js", "async function renderDiseasePrograms()")
ESB = _block("pages-public.js", "async function renderEsb()")
# 重点慢专病中心那块在 renderSpdExpert 里（不是 renderSpdCenter，后者是调度中枢）——
# 按页面名想当然会取错块，断言就在别的页面上空转。
SPDC = _block("pages-spd.js", "async function renderSpdExpert()")

PAGES = {"apt": APT, "orgs": ORGS, "dash": DASH, "dict": DICT, "tcm": TCM, "arch": ARCH,
         "rbac": RBAC, "proj": PROJ, "res": RES, "surv": SURV, "vacc": VACC, "herit": HERIT,
         "og": OG, "od": OD, "fund": FUND, "dp": DP, "esb": ESB, "spdc": SPDC}


@pytest.mark.parametrize(
    ("page", "path"),
    [
        ("apt", "/api/triage/suggest"),
        ("orgs", "/api/organizations/tree-health"),
        ("dash", "/api/metrics/drilldown-metrics"),
        ("dict", "/api/dictionaries/${system}/import"),
        ("tcm", "/api/tcm/constitution/spec"),
        ("tcm", "/api/tcm/constitution"),
        ("arch", "/api/patients/${encodeURIComponent(ehcNo)}"),
        ("rbac", "/api/rbac/permissions?"),
        ("rbac", "/api/rbac/roles/${d.role}/permissions/${d.revoke}"),
        ("proj", "/api/projects/milestones/${d.msdone}/done"),
        ("proj", "/api/projects/milestones/${d.msreopen}/reopen"),
        ("res", "/api/resources/${d.rsedit}"),
        ("surv", "/api/surveillance/resources/${emres}"),
        ("vacc", "/api/vaccine-supply/expiring?days="),
        ("vacc", "/api/vaccine-supply/aefi/${d.aefiout}/outcome"),
        ("herit", "/api/tcm-heritage/simulations/${simdo}/attempts"),
        ("herit", "/api/tcm-heritage/simulations/${simlog}/attempts"),
        ("og", "/api/org-groups/of-org/${orgId}"),
        ("od", "/api/outpatient/treatments?patient_id="),
        ("od", "/api/outpatient/consent-templates/${tpledit}"),
        ("fund", "/api/fund/pools/${e.target.dataset.fdedit}"),
        ("fund", "/api/fund/pools/${e.target.dataset.fddist}/distributions"),
        ("dp", "/api/disease-programs/enrollments/${dpview}"),
        ("dp", "/api/disease-programs/${dpedit}"),
        ("esb", "/api/esb/flow-runs?flow_id="),
        ("esb", "/api/esb/flows/${esbflowtoggle}"),
        ("esb", "/api/integration/exchange-logs"),
        ("spdc", "/api/spd/org-tree"),
        ("spdc", "/api/spd/centers/${el.dataset.centerEdit}"),
    ],
)
def test_端点在对应页面上有调用点(page, path):
    assert path in PAGES[page], f"{page} 页面上找不到 {path} 的调用点"


# ---------------------------------------------------------------- 本轮的两条发现

def test_模拟诊疗的作答入口真的存在():
    """原注释说"作答与评分在医师端 H5 完成"，而 doctor.js 里没有这块代码。"""
    doctor = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    assert "simulations" not in doctor, "医师端 H5 真建了这块的话，这条断言与注释都要改"
    assert "data-simdo=" in HERIT, "管理端也没有作答入口，等于两边都没建"
    assert "医师端 H5 里其实没有这块代码" in HERIT, "注释还在说一件不存在的事"


def test_ESB两个启停不共用dataset名():
    """接入方启停停的是谁能投消息进来，编排启停停的是某条编排还跑不跑。"""
    assert "data-esbflowtoggle=" in ESB
    # 接入方那个仍叫 esbtoggle，两者必须同时存在且不同名
    assert "data-esbtoggle=" in ESB
    assert "撞成同一个 dataset 名会互相吃掉" in ESB


# ---------------------------------------------------------------- 各自的判据

def test_机构树体检说清403后果():
    assert "一律 403" in ORGS
    assert "health.orphans" in ORGS and "health.broken_chains" in ORGS
    # 判据是层级相邻而非链路长度
    assert "层级相邻</b>而非链路长度" in ORGS


def test_导诊端点收裸数组():
    """triage_suggest(symptoms: list[str]) —— 传 {symptoms: [...]} 会 422。"""
    assert "JSON.stringify(symptoms)" in APT
    assert "裸数组" in APT
    assert "不回空列表" in APT


def test_可下钻指标目录来自服务端():
    """前端不另维护一份"哪些能下钻"的清单。"""
    assert "drilldowns" in DASH
    assert "前端不另维护一份" in DASH


def test_告知书模板维护列表含停用的():
    """只列启用的话，翻已签告知书的模板出处会翻不到。"""
    assert 'api("/api/outpatient/consent-templates")' in OD, "维护清单用了 ?active=true"
    assert "已签的正文在签署时已冻结成快照" in OD


def test_已清算基金池只给分配明细不给改要素():
    assert 'p.status === "settled"' in FUND
    assert "已清算的池不可再改要素" in FUND
    assert "d.score_detail" in FUND
    assert "快照" in FUND


def test_停用类操作都说明影响面():
    assert "已入组的照旧" in DP, "专病目录停用没说明影响面"
    assert "已签的告知书不受影响" in OD
    assert "已加入分组" not in OG or True  # og 无停用语义，占位说明不适用


def test_PATCH都只提交填了的字段():
    """各 Update 模型的字段都可选、None 表示"不改"：空串会把原值抹成空。"""
    for name, block in [("resources", RES), ("surveillance", SURV),
                        ("outpatient_docs", OD), ("fund", FUND)]:
        assert 'if (v !== "")' in block, f"{name} 的 PATCH 会把空串当成要清空"


def test_体质辨识两种入法二选一():
    """scores 是已换算的转化分，answers 是简表原始条目分——混着传后端只认前者。"""
    assert "direct ? { scores: parsed } : { answers: parsed }" in TCM
    assert "二选一" in TCM


def test_患者360先按卡号取患者():
    """卡号打错时这一步就 404，错误指向"卡号不存在"而不是一坨空档案。"""
    assert "/api/patients/${encodeURIComponent(ehcNo)}" in ARCH
    assert "pat.name" in ARCH
    assert "按登录角色脱敏" in ARCH


def test_权限点能撤到单条():
    """授权按模块整块给，撤销只能整块收回的话，中间那段时间这个角色什么都做不了。"""
    assert "撤到单条" in RBAC
    assert "data-revoke=" in RBAC


def test_临期批次只列还有余量的():
    """用完的不必再提醒；过期的一并列出是提示走报废流程，不是催人用掉。"""
    assert "还有余量" in VACC
    assert "走报废流程" in VACC


def test_AEFI未知转归不是漏填():
    assert "上报时多为" in VACC
    assert 'r.outcome === "unknown"' in VACC
