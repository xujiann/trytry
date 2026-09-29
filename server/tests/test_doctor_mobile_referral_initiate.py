"""医生移动端「转诊办理」能发起上转、看得到本人被退回的单子与退回意见（P2-795，第二十一批「页面给出的动作 vs 后端允许的
角色与状态」扫描 N4-3 的移动端余项；P2-794 修的是按钮按谁能办摆）。

村医手册写「在『转诊办理』里发起上转，写清理由」「转诊单退回：看退回理由，补材料后重新发起」；手机上的转诊办理原先只取
在途的单子、只有审核 / 到院 / 承接按钮——发起要回电脑上的管理端，退回的单子（已结束）从清单里消失，退回意见在轨迹里、
手机上看不到。行为由端到端用例 `test_医生移动端_村医发起上转_退回后看意见再重新发起` 驱动一遍；这里钉住形状与手册。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCTOR_JS = (ROOT / "server" / "app" / "static" / "m" / "doctor.js").read_text(encoding="utf-8")
MANUAL = (ROOT / "docs" / "培训手册" / "村医手册.md").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = DOCTOR_JS.index(f"async function {name}(")
    return DOCTOR_JS[start:DOCTOR_JS.index("\n}\n", start)]


def test_发起上转从本人名下的在管患者里选_理由必填():
    form = _function("openSpdReferralForm")
    assert '<select name="enrollment" required>' in form
    assert 'api("/api/organizations?level=county")' in form   # 目标机构从县级机构里选（可空）
    assert '<textarea name="reason" rows="2"' in form and "required" in form
    assert 'spdPost("/api/spd/referrals", { patient_id: Number(patientId), program_code: programCode,' in form
    assert 'direction: "up"' in form


def test_转诊办理页有发起入口_本人被退回的单子单列():
    page = _function("loadSpdReferral")
    assert 'api("/api/spd/referrals?status=rejected&limit=50")' in page   # 修前只取在途的
    assert "rejected.filter((r) => r.initiator_id === me.id)" in page
    assert "`/api/spd/enrollments?limit=100&${mine}`" in page   # 与「我的患者」同一口径（P2-373）
    for attr in ("data-spd-ref-new", "data-spd-reject-view", "data-spd-ref-again"):
        assert attr in page, attr   # 修前一个都没有
    assert 'x.action === "reject"' in page   # 退回意见取轨迹里最近一次「退回」的意见


def test_手册写的入口与页面一致():
    assert "在「转诊办理」里点**发起上转**" in MANUAL
    assert "「我发起的、被退回的」里点**看退回意见**" in MANUAL and "点**重新发起**" in MANUAL
    assert "<p class=\"hint\">我发起的、被退回的" in DOCTOR_JS
    assert ">发起上转</button>" in DOCTOR_JS and ">看退回意见</button>" in DOCTOR_JS and ">重新发起</button>" in DOCTOR_JS
