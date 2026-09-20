"""会诊 / 医疗质量 / 慢专病路径·服务包·转诊 的界面入口守卫（P1-40，五模块清零）。

全平台棘轮只回答"这条路径有没有人调用过"。这五块各有它看不见、而写错了后果
不轻的事：

* **已完成但未计费的会诊，统计说得出有几例、说不出是哪几例**：`/stats` 只给
  `unsettled_count`，经办拿着那个数字没法动手。所以 `ConsultationOut` 补了
  `fee/fee_settled/fee_note` 三个字段（新增，向后兼容），行上直接看得见。
  `fee=0` 与"未计费"是两回事（院内会诊常不计费），不能拿 0 当哨兵。
* **环节质控规则改了不会重算既有病历**：界面必须说，否则改完扣分看着没生效，
  下一个人会再改一遍。
* **"复评"与"看原文"是两件事**：`/records/{id}/qc` 按当前规则重算并回写，
  `/records/{id}` 读的是评分时的缺陷快照。混成一个按钮，规则改过之后就说不
  清看到的是哪一个。
* **已发布的路径不可直接改节点**：在跑的实例会突然多出一个没人知道的任务，
  正规途径是复制新版本。界面要把这句说出来，而不是让人对着 409 猜。
* **PATCH 收裸 dict**：把空串一并发上去会把原来有值的字段清空——改一个时限
  顺手抹掉科室，事后没人查得出。只提交填了的字段。
* **转诊规则试算默认不开单**：`auto_create=true` 命中即开单，一次批量随访
  录入能开出几十张。勾它要再确认一次。
* **删路径模板的真正判据是"有没有实例引用"**，不是发布状态：按 status 猜着
  禁用，会把"已发布但没人用过"这种可以删的挡在外面。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
CORE = (STATIC / "core.js").read_text(encoding="utf-8")
CLINICAL = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
SPD = (STATIC / "pages-spd.js").read_text(encoding="utf-8")


def _block(src: str, fn: str) -> str:
    """取一个 render 函数的源码，**到下一个 render 函数为止**。

    按函数名定位而不是全文搜——"这串字在文件里出现过"证明不了"它在这个页面
    上"。固定字符数截断同样不行：`renderSpdPath` 与 `renderSpdReferral` 只隔
    一万多字符，取宽了就会把下一页的调用点算成本页的，断言看着绿、变异照样
    不红。所以边界取下一个 `async function render`。
    """
    start = src.index(fn)
    rest = src[start + len(fn):]
    marker = "\nasync function render"
    end = start + len(fn) + rest.index(marker) if marker in rest else len(src)
    return src[start:end]


CONS = _block(CORE, "async function renderConsultations()")
QUAL = _block(CLINICAL, "async function renderQuality()")
# 慢专病三块各自一个页面，**分开取**：合成一个"整份 pages-spd.js"会让断言
# 退化成"这串字在文件里出现过"——路径调用点写在别的页面上照样算过。
PATH = _block(SPD, "async function renderSpdPath()")
PATIENTS = _block(SPD, "async function renderSpdPatients()")
REF = _block(SPD, "async function renderSpdReferral()")

PAGES = {"cons": CONS, "qual": QUAL, "path": PATH, "patients": PATIENTS, "ref": REF}


@pytest.mark.parametrize(
    ("page", "path"),
    [
        ("cons", "/api/consultations/experts"),
        ("cons", "/api/consultations/stats"),
        ("cons", "/api/consultations/${id}/fee"),
        ("qual", "/api/quality/infection-stats"),
        ("qual", "/api/quality/record-qc-rules"),
        ("qual", "/api/quality/record-qc-rules/${d.qcrtoggle}"),
        ("qual", "/api/quality/records/${d.mrview}"),
        ("path", "/api/spd/path-templates/${templateId}"),
        ("path", "/api/spd/path-templates/${tplDel.dataset.tplDel}"),
        ("path", "/api/spd/path-nodes/${nodeEdit.dataset.nodeEdit}"),
        ("path", "/api/spd/path-nodes/${nodeDel.dataset.nodeDel}"),
        ("patients", "/api/spd/enrollments/${enrId}/packages"),
        ("patients", "/api/spd/package-bindings/${pkgUnbind.dataset.pkgUnbind}/unbind"),
        ("patients", "/api/spd/package-bindings/${pkgUse.dataset.pkgUse}/usages"),
        ("patients", "/api/spd/package-bindings/${pkgUsages.dataset.pkgUsages}/usages"),
        ("ref", "/api/spd/referral-rules/check"),
        ("ref", "/api/spd/referral-rules/${ruleEdit.dataset.refruleEdit}"),
        ("ref", "/api/spd/referrals/${detail.dataset.refDetail}"),
        ("ref", "/api/spd/referrals/${withdraw.dataset.refWithdraw}/withdraw"),
    ],
)
def test_端点在对应页面上有调用点(page, path):
    assert path in PAGES[page], f"{page} 页面上找不到 {path} 的调用点"


# ---------------------------------------------------------------- 会诊

def test_会诊行上看得见计费状态():
    """统计只给"已完成但未计费 N 例"，说不出是哪几例。"""
    assert "c.fee_settled" in CONS, "行上没有计费状态，未计费的挑不出来"
    assert 'data-act="fee"' in CONS


def test_会诊计费把零元当成一次真实记录():
    """fee=0 是"本次不计费"的真实记录，不是"没填"——空串取消、"0" 照样提交。"""
    assert 'if (fee === null || fee === "") return;' in CONS, "用 falsy 判空会把 0 一起吞掉"


def test_会诊统计把未评价未计费单列而不并进分母():
    """把未评价当 0 分，会让评价率越低分数越难看，最后逼出来的是"催评分"。"""
    assert "stats.rating.unrated_count" in CONS
    assert "stats.fee.unsettled_count" in CONS
    assert "stats.caliber" in CONS, "口径说明没渲染"


def test_会诊计费字段随列表一起出():
    from app.schemas import ConsultationOut

    assert {"fee", "fee_settled", "fee_note"} <= set(ConsultationOut.model_fields)


# ---------------------------------------------------------------- 医疗质量

def test_质控规则改动不会自动重算要说出来():
    """改完扣分看着没生效，下一个人会再改一遍。"""
    assert "既有病历不会自动重算" in QUAL
    assert QUAL.count("既有病历不会自动重算") >= 2, "停用与改扣分两条路径都要说"


def test_复评与看原文是两条路():
    """/qc 按当前规则重算并回写，/records/{id} 读评分时的缺陷快照。"""
    assert "data-mrqc=" in QUAL and "data-mrview=" in QUAL
    assert "detail.defects" in QUAL, "看原文没渲染缺陷快照"
    # 快照那条不能改成重算
    seg = QUAL[QUAL.index("if (d.mrview)"):QUAL.index("if (d.qcrtoggle)")]
    assert "/qc`" not in seg


def test_院感待核实单列():
    """待核实的既不算确认也不算排除——挂着不处理，区域提醒就一直缺这部分。"""
    assert "infStats.pending_verify" in QUAL
    assert "infStats.by_site" in QUAL


# ---------------------------------------------------------------- 慢专病

def test_已发布路径不可改节点的理由写在界面上():
    assert "已发布的路径不可直接改节点" in PATH
    # 已发布时不给出改/删按钮（后端一律 409）
    assert 'const editable = d.status !== "published";' in PATH


def test_删路径模板不按发布状态猜():
    """能不能删的判据是"有没有患者实例引用"，列表里没有这个字段，后端以 409 作答。"""
    assert "已被患者实例引用的路径删不掉" in PATH
    seg = PATH[PATH.index('data-tpl-view='):PATH.index('data-tpl-view=') + 600]
    assert 'data-tpl-del="${t.id}">删除' in seg
    assert 'status === "published" ? "" : `<button class="btn danger" data-tpl-del' not in PATH


def test_节点与规则的PATCH只提交填了的字段():
    """PATCH 收裸 dict，空串一并发上去会把原来有值的字段清空。"""
    probe = 'for (const [k, v] of Object.entries(form)) if (v !== "" && v !== undefined) body[k] = v;'
    assert probe in PATH, "路径节点 PATCH 会把空串当成要清空"
    assert probe in REF, "转诊规则 PATCH 会把空串当成要清空"


def test_解绑不删流水():
    """已经发生的服务不能因为解绑就从账上消失。"""
    assert "已发生的扣减流水保留" in PATIENTS
    # 解绑后仍可查流水
    assert 'data-pkg-usages="${b.id}">扣减流水' in PATIENTS


def test_规则试算勾了自动开单要再确认():
    """一次批量随访录入能开出几十张单子。"""
    assert 'const autoCreate = f.get("auto_create") === "on";' in REF
    assert "if (autoCreate && !confirm(" in REF


def test_撤回按钮只在可撤状态出现():
    """只有发起人本人、且尚未被上级接收时可撤（后端否则 403/409）。
    `station_reviewed` 是 ADR-0005 前四级链的存量在途状态，与 submitted 同样可撤。"""
    assert '["submitted", "station_reviewed"].includes(c.status)' in REF
    assert 'data-ref-withdraw="${c.id}"' in REF


def test_转诊详情渲染触发依据与轨迹():
    """患者端 #16 要的是"查看触发依据、异常指标、提交资料和处理意见"。"""
    assert "c.trigger_evidence" in REF
    assert "c.materials" in REF
    assert "(c.steps || [])" in REF
