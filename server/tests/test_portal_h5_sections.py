"""居民端 H5 四块新增内容的入口守卫（P1-40，portal 12 → 0）。

全平台棘轮（`test_frontend_endpoint_coverage.py`）只回答"这条路径有没有人调用
过"。居民端还有几件它看不见、而写错了后果不轻的事：

* **哪几块只能给本人看**：更正/注销申请与账号绑定是按**账户**归集的，代管的
  家庭成员没有自己的账户——挂在成员档案下就是张冠李戴。这与既有的
  「谁看过我的档案」是同一条判断（`viewingPatientId === null`）。
  疾病管理档案与知情同意则相反：后端收 `patient_id`，代管成员看得到。
* **绑定验证码必须是 `purpose=bind`**：登录验证码不能拿来改绑，否则钓到一个
  登录码就能把别人的账户绑到自己手机上。
* **管理端的取证工具（`scripts/render_diff.js`）只覆盖管理端**，渲染不了
  `m/m.js`，所以这一页的入口用静态断言守，不假装有渲染取证。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
M_JS = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
M_HTML = (STATIC / "m" / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path",
    [
        "/api/portal/me/enrollments/all",
        "/api/portal/me/consents",
        "/api/portal/me/corrections",
        "/api/portal/me/deposits",
        "/api/portal/auth/bind-phone",
        "/api/portal/auth/bind-wechat",
    ],
)
def test_六个端点都在居民端调到(path):
    assert path in M_JS, f"居民端没有调用 {path}"


@pytest.mark.parametrize(
    "box",
    ["archive-enrollments", "archive-consents", "archive-corrections", "archive-bindings"],
)
def test_四个容器都在页面里(box):
    assert f'id="{box}"' in M_HTML, f"{box} 容器不在 index.html 里，渲染没有落点"


def test_按账户归集的两块只在本人视角渲染():
    """更正申请与账号绑定按账户归集，代管成员没有自己的账户。

    盯的是那两个渲染调用**被条件包着**，而不是"文件里出现过这个判断"——
    `viewingPatientId === null` 在别处本来就有好几处（这一坑在
    `test_frontend_call_site_coverage.py` 里已经踩过一次，那次第一版断言是空洞的）。
    """
    idx = M_JS.index("renderMyCorrections(), renderBindings(me)")
    around = M_JS[max(0, idx - 300):idx]
    assert "viewingPatientId === null" in around, (
        "更正申请与账号绑定没有锁在本人视角——代管成员没有自己的账户，"
        "把它们挂在成员档案下就是张冠李戴"
    )


def test_代管视角下这两块要清空():
    """不只是"不渲染"——切回成员时得把上一次本人视角的内容擦掉，否则残留。"""
    assert '$("#archive-corrections").innerHTML = ""' in M_JS, (
        "切到代管成员时没清空更正申请区块，会残留本人的申请记录"
    )
    assert '$("#archive-bindings").innerHTML = ""' in M_JS, (
        "切到代管成员时没清空账号绑定区块"
    )


def test_疾病管理档案与知情同意支持代管成员():
    """这两块与上面相反：后端收 patient_id，代管成员应当看得到自己的。"""
    for fn in ("renderMyEnrollments", "renderMyConsents"):
        start = M_JS.index(f"async function {fn}(")
        end = M_JS.index("\n}", start)
        body = M_JS[start:end]
        assert "viewingPatientId === null ? \"\" :" in body, (
            f"{fn} 没有按代管视角传 patient_id——代管成员看不到自己的那一份"
        )


def test_绑定验证码用的是bind用途():
    """登录验证码不能拿来改绑：钓到一个登录码就能把别人的账户绑到自己手机上。"""
    idx = M_JS.index('"/api/portal/auth/bind-phone"')
    around = M_JS[max(0, idx - 900):idx]
    assert 'purpose: "bind"' in around, (
        "绑定手机号用的不是 purpose=bind 的验证码"
    )


def test_押金余额与流水都渲染():
    idx = M_JS.index('"/api/portal/me/deposits"')
    around = M_JS[idx:idx + 700]
    assert "dep.balance" in around, "只列了流水没给当前余额——居民最常问的就是还剩多少"
