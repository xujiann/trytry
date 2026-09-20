"""药品召回 / 审方规则 / 消供申领 / 一码通 / 死因报告卡 / 物资调拨 的界面入口守卫
（P1-40，七模块清零）。

全平台棘轮只回答"这条路径有没有人调用过"。这一批里最该单说的是**药品召回**：
后端把召回链写得很完整（余量同事务退出可用汇总、按批号反查发给了谁），而界面
一个入口都没有——召回在真环境里只能靠手写 curl。`batch_dispense_trace` 的
docstring 原话是"召回时唯一有用的那个查询"。

各自看不见、而写错了后果不轻的事：

* **召回不是只翻状态**：余量要同事务退出可用汇总（记到 blocked_quantity 上，
  药还在库房、只是发不出去）。只翻 status 的话，召回的药一直被算成有货，
  缺药预警与采购建议长期少报。界面要把这句说出来。
* **召回前先摆流向**：召回本身只是不让它再发出去；已经在患者手上的那部分要
  靠流向名单去联系。先召回再想起来查，名单还在，但人已经晚了一步。
* **审方规则停用不删行**：规则改过什么、什么时候不再生效，处方点评复核时要
  回溯得到。所以列表要 include_inactive，停用的也要看得见、能重新启用。
* **批量导入一条撞车整批回滚**：格式错在前端就拦下来，别让人把 200 行提上去
  再从 500 里倒推是哪一行写错了。
* **一码通的过期与伪造是两回事**：410 让人重新出码，403 是伪造，处置完全不同。
  统一成"核验失败"就把这个区分丢了。
* **死因报告卡没有直连专线**：导出供手工网报或前置机对接，界面要写明，别让人
  以为点一下就报上去了。
* **`POST /api/mgmt/secondments/{id}/end` 不补界面，改标 deprecated**：它是
  `/api/staffing/secondments/{id}/end` 的劣化重复（详见该 handler 的 docstring）。
"""
import re
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
CORE = (STATIC / "core.js").read_text(encoding="utf-8")
CLINICAL = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
MGMT = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
PUBLIC = (STATIC / "pages-public.js").read_text(encoding="utf-8")


def _block(src: str, fn: str) -> str:
    """取一个 render 函数的源码，到下一个 render 函数为止。

    固定字符数截断不行：取宽了会把下一页的调用点算成本页的，断言看着绿、
    变异照样不红。
    """
    start = src.index(fn)
    rest = src[start + len(fn):]
    marker = "\nasync function render"
    return src[start:start + len(fn) + rest.index(marker)] if marker in rest else src[start:]


PHARM = _block(CORE, "async function renderPharmacy()")
RX = _block(CORE, "async function renderRx()")
CSSD = _block(CORE, "async function renderCssd()")
CRED = _block(MGMT, "async function renderCredentials()")
CERTS = _block(PUBLIC, "async function renderCerts()")
HRF = _block(CLINICAL, "async function renderHrFinance()")

PAGES = {"pharm": PHARM, "rx": RX, "cssd": CSSD, "cred": CRED, "certs": CERTS, "hrf": HRF}


@pytest.mark.parametrize(
    ("page", "path"),
    [
        ("pharm", "/api/pharmacy/purchase-suggestions"),
        ("pharm", "/api/pharmacy/batches/${brecall}/recall"),
        ("pharm", "/api/pharmacy/batches/${batchId}/dispenses"),
        ("rx", "/api/prescriptions/rules/import"),
        ("rx", "/api/prescriptions/rules/${encodeURIComponent(ruleoff)}"),
        ("rx", "/api/prescriptions/rules/${encodeURIComponent(ruleon)}/reactivate"),
        ("cssd", "/api/cssd/requests"),
        ("cssd", "/api/cssd/requests/${cssdfill}/fulfill?batch_id="),
        ("cred", "/api/credentials/one-code"),
        ("cred", "/api/credentials/one-code/resolve"),
        ("cred", "/api/credentials/resolve?identifier="),
        ("certs", "/api/certs/death-report-cards/export.csv?"),
        ("certs", "/api/certs/${deathcard}/death-report-card"),
        ("certs", "/api/checkups/${chkitems}/items"),
        ("certs", "/api/checkups/${chkreview}/review"),
        ("hrf", "/api/mgmt/assets/${d.assetxfer}/transfer?to_org_id="),
        ("hrf", "/api/mgmt/assets/${d.assetscrap}/scrap"),
    ],
)
def test_端点在对应页面上有调用点(page, path):
    assert path in PAGES[page], f"{page} 页面上找不到 {path} 的调用点"


# ---------------------------------------------------------------- 药品召回

def test_召回前先把流向摆出来():
    """召回只是不让它再发出去；已经在患者手上的那部分要靠这张名单去联系。"""
    seg = PHARM[PHARM.index("if (brecall)"):]
    assert "await drawTrace(brecall);" in seg
    # 流向必须在"问原因"之前拉出来，否则等于先召回再想起来查
    assert seg.index("drawTrace(brecall)") < seg.index("prompt(")


def test_召回说明余量退出可用汇总():
    """只翻 status 不动汇总，召回的药会一直被算成有货，缺药预警长期少报。"""
    assert "余量同事务退出可用汇总" in PHARM
    assert "blocked_quantity" in PHARM


def test_召回必填原因():
    assert 'const reason = prompt("召回原因（必填' in PHARM
    assert "if (!reason) return;" in PHARM


def test_流向名单区分在患者手上与已冲销():
    """冲销的不计入"仍在外面"的量，但行保留——两者混为一谈，
    召回时会按已经退回来的那批去打电话。"""
    assert "在患者手上" in PHARM and "已冲销" in PHARM
    assert "t.total_dispensed" in PHARM


# ---------------------------------------------------------------- 审方规则

def test_规则列表带停用的():
    """停用不删行，列表不带 include_inactive 的话停用过的规则就此消失，
    没法重新启用、也看不出"这条曾经生效过"。"""
    assert "/api/prescriptions/rules?include_inactive=true" in RX
    assert "data-ruleon=" in RX and "data-ruleoff=" in RX


def test_停用不删行写在界面上():
    assert "停用<b>不删行</b>" in RX


def test_批量导入先在前端校验格式():
    """后端一条撞车整批回滚，返回是 500 与"一条都没进"。"""
    assert "整批未提交" in RX
    assert "r.imported" in RX and "r.updated" in RX, "没把新建/更新两个数分开报"


# ---------------------------------------------------------------- 消供申领

def test_申领响应说明批次状态要求():
    """拿灭菌中的批次去响应，等于把还没灭完菌的器械算成已经给出去了。"""
    assert "已灭菌或已发放" in CSSD


def test_申领响应的批次走query():
    """后端签名收的是 query 参数，不是请求体。"""
    assert "/fulfill?batch_id=${Number(batchId)}" in CSSD


# ---------------------------------------------------------------- 一码通

def test_一码通核验区分过期与伪造():
    """410 让人重新出码，403 是伪造，处置完全不同。"""
    assert "过期（410）与签名错误（403）是两回事" in CRED
    # 原样显示后端的话，不统一成"核验失败"
    seg = CRED[CRED.index('$("#onecode-resolve")'):]
    # 判的是"把后端的话原样交出去"，不是"某个词没出现过"——上面那句注释里
    # 就写着这个词，按裸子串判会匹配到注释自己。
    assert 'setMsg("#onecode-msg", err.message, false);' in seg


def test_一码通说明不落库与短时效():
    assert "不落库" in CRED
    assert "给得太长等于回到静态码" in CRED


def test_多卡协同注明命中类型():
    """不注明的话，同一个人从不同介质进来看不出差别。"""
    assert "r.matched_by" in CRED
    assert "credential_no:" in CRED and "ehc_no:" in CRED and "id_card:" in CRED


# ---------------------------------------------------------------- 死因报告卡 / 体检

def test_死因报告卡写明无直连专线():
    """别让人以为点一下就报上去了。"""
    assert "无直连专线" in CERTS
    assert "手工网报" in CERTS
    assert "已按角色脱敏" in CERTS


def test_死因报告卡按钮只对死亡证明给出():
    """后端对非死亡证明一律 422。"""
    assert 'c.cert_type === "death" ?' in CERTS


def test_体检分项用result_value而不是result():
    """CheckupItemOut 的字段是 result_value；写成 result 会渲染出一片 undefined，
    路径调用点照样在，棘轮照样绿——这类字段名错棘轮看不见。"""
    from app.routers.checkups import CheckupItemOut

    assert "result_value" in CheckupItemOut.model_fields
    assert "i.result_value" in CERTS
    assert "esc(i.result)" not in CERTS


def test_总检重复按覆盖要说出来():
    assert "重复总检按覆盖" in CERTS


# ---------------------------------------------------------------- 物资

def test_物资调拨与报废():
    assert "已报废的物资不可再调拨" in HRF
    assert "调出方必须是本机构" in HRF
    # 报废不可逆，先确认
    seg = HRF[HRF.index("if (d.assetscrap)"):]
    assert "confirm(" in seg


def test_报废物资不再给调拨按钮():
    """后端对已报废物资的调拨一律 409。"""
    assert 'a.status !== "scrapped"' in HRF


# ---------------------------------------------------------------- 废弃的重复端点

def test_mgmt的结束派驻已标废弃而不是补界面():
    """同一张 secondments 表上两条"结束派驻"，mgmt 那条是劣化的：end_date 必填、
    不校验早于开始日期、无条件把员工状态置 active。补界面等于把缺陷接出去。"""
    # 从 router 对象上读，不从 app.routes：后者在这个仓库里拿不到全量
    # （只有 99 条），按它判会 KeyError 而不是判出结论。
    from app.routers import admin_mgmt, staffing as staffing_mod

    def _find(module, path: str):
        for router in (v for v in vars(module).values() if isinstance(v, APIRouter)):
            for route in router.routes:
                if isinstance(route, APIRoute) and route.path.endswith(path):
                    return route
        raise AssertionError(f"没找到 {path}")

    mgmt = _find(admin_mgmt, "/secondments/{secondment_id}/end")
    staffing = _find(staffing_mod, "/secondments/{secondment_id}/end")
    assert mgmt.deprecated is True, "劣化的那条没标废弃"
    assert not getattr(staffing, "deprecated", False), "留用的那条不该被标废弃"
    # 界面上没人去调废弃的**那一条**。判据只能收在 `/end` 上：同模块的
    # `/secondments/stats` 与 `POST /secondments`（建派驻）都在正常使用，
    # 按前缀判会把它们一起误伤。
    hits = re.findall(r"/api/mgmt/secondments/[^\"'`\s]*/end", CLINICAL + MGMT + CORE + PUBLIC)
    assert hits == [], f"界面上仍在调废弃的那条：{hits}"
