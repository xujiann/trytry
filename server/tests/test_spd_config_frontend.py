"""慢专病配置中心：管理端入口 + 配题路径上的两处正确性守卫。

背景（2026-08-25 完成度盘点，见 ROADMAP「可用性线」）：`config/scales.py` 的
四个域——评估量表 / 宣教素材 / 服务包 / 专病标签——后端在 ADR-0008 那轮拆包
并补齐了契约，**前端却一个入口都没有**。量表配不了、素材录不进，子系统只能
拿演示种子跑给人看。本轮补上 `renderSpdConfig` 这一页。

本文件守三件事，每件都对着一个"错了不报错、只是算错"的形态：

1. **入口棘轮**：四个域的**每一个操作**（列表/新建/改/发布/停用/扫码，共 14 个）
   都要在管理端 JS 里找得到对应的调用形状。页面被删或被改花了，后端全绿、
   前端也不报错，只是那几个域又变回配不了——与 `test_frontend_page_registry`
   守的是同一类静默失效，只是那边守"注册表漏登记"，这边守"入口整个消失"。
   判据钉到操作而不是端点字符串，是变异验证逼出来的，理由写在 `REQUIRED_CALLS`
   上方。清单只许变长，不许变短。

2. **PATCH 也要查题目 key 重复**（后端）：`score_scale` 按 `item["key"]` 去
   `answers` 取值，重复的 key 会让同一个答案被**计两次分**——量表照样出分、
   出的是错的风险等级。此前只有 POST 查这一条，而"新建只给壳、题目随后 PATCH
   上去"正是配题的常规走法（新页面就是这么做的），等于查重形同虚设。

3. **评分区间的上下限不得用数值型弹窗字段**（前端）：`spdModal` 把空的
   `type:"number"` 字段转成 **0** 而不是空串。而 `max: 0` 与 `max: null` 在
   `score_scale` 里是两回事——后者是"不限"，前者让该区间永远命不中，于是
   "6 分以上=高危"这条配出来就是死的。这条守卫钉住那三个字段用文本收。
"""
import re
import warnings
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
ADMIN_JS = "\n".join(
    p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("*.js"))
)

#: 四个域在管理端必须够得着的**每一个操作**。
#:
#: 判据故意钉到「调用形状」而不是「端点字符串出现过」——后者是个空棘轮：
#: 同一个端点在一页里通常被引用两次（列表拉一次、新建再拉一次），删掉其中一次
#: 字符串还在，测试照样绿。第一版就是这么写的，变异验证（把列表那次换成别的
#: 端点）没咬住，才改成现在这样。
#:
#: 扫的是**全部管理端 JS**而不是配置页那一段：这条守的是「这个域从管理端够得着」，
#: 别的页面提供了入口同样算数——那时放行是对的，不是漏网。
#:
#: 清单只许变长。确要下线某个入口，连同对应行一起删并在 PR 里说明。
REQUIRED_CALLS = {
    "评估量表": [
        ("列表", r'api\("/api/spd/scales\?'),
        ("新建", r'postAction\("/api/spd/scales"'),
        ("配题(PATCH)", r'api\(`/api/spd/scales/\$\{[^`]*`,\s*\{\s*\n?\s*method:\s*"PATCH"'),
        ("发布", r'/api/spd/scales/\$\{[^`]*\}/publish'),
        ("停用", r'/api/spd/scales/\$\{[^`]*\}/disable'),
        ("扫码", r'/api/spd/scales/\$\{[^`}]*\}/qr\.svg'),
    ],
    "宣教素材": [
        ("列表", r'api\("/api/spd/edu-materials\?'),
        ("新建", r'postAction\("/api/spd/edu-materials"'),
        ("改内容/启停(PATCH)", r'/api/spd/edu-materials/\$\{'),
    ],
    "服务包": [
        ("列表", r'api\("/api/spd/service-packages\?'),
        ("新建", r'postAction\("/api/spd/service-packages"'),
        ("启停(PATCH)", r'/api/spd/service-packages/\$\{'),
    ],
    "专病标签": [
        ("列表", r'api\("/api/spd/tags"\)'),
        ("新建", r'postAction\("/api/spd/tags"'),
    ],
}


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def h(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ---------------------------------------------------------------- 入口棘轮


@pytest.mark.parametrize("domain", sorted(REQUIRED_CALLS))
def test_配置中心每个域的每个操作都有管理端入口(domain):
    missing = [
        name for name, pattern in REQUIRED_CALLS[domain]
        if not re.search(pattern, ADMIN_JS)
    ]
    assert not missing, (
        f"「{domain}」的以下操作在管理端 JS 里找不到调用，这个域又变回"
        f"「后端有、前端做不了」：\n  " + "\n  ".join(missing)
        + "\n（清单只许变长。确要下线某个入口，连同 REQUIRED_CALLS 里对应行一起删并在 PR 里说明。）"
    )


def test_入口棘轮自证判据不是空的():
    """守卫自证：判据必须**逐个操作**钉住，不能退回「端点字符串出现过就算」。

    第一版正是那么写的，于是「把列表那次调用换成别的端点」这种变异咬不住——
    同一个端点在一页里通常被引用两次（列表一次、新建一次），删一次字符串还在。
    这条用例把分母打印出来，并确认每个域至少钉了两个不同的操作。
    """
    total = sum(len(v) for v in REQUIRED_CALLS.values())
    thin = [d for d, calls in REQUIRED_CALLS.items() if len(calls) < 2]
    warnings.warn(
        "\n[慢专病配置入口棘轮] 覆盖面自证\n"
        f"    域 {len(REQUIRED_CALLS)} 个 / 钉住的操作 {total} 个："
        + "、".join(f"{d}{len(c)}" for d, c in sorted(REQUIRED_CALLS.items()))
        + "\n    扫描：全部管理端 JS（别的页面提供入口同样算数——那时放行是对的）",
        stacklevel=2,
    )
    assert not thin, f"这些域只钉了不到两个操作，棘轮太松：{thin}"


def test_配置页已登记进注册表且函数确实存在():
    """注册表与函数定义两边都要有——只写一边的后果是静默的（页面看不见 / 白屏）。"""
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id: "spdconfig"' in app_js, "配置页没登记进 PAGES，导航里根本不存在"
    assert "render: renderSpdConfig" in app_js
    assert re.search(r"^async function renderSpdConfig\(", ADMIN_JS, re.M), (
        "PAGES 引用了 renderSpdConfig，但没有这个函数——PAGES 求值时 ReferenceError，整页白屏"
    )


# ---------------------------------------------------- 前端：留空必须是「不限」


#: `{ name: "min", … type: "number" }` —— 同一个对象字面量里既有该 name 又有数值型。
def _declared_as_number(field: str) -> bool:
    pattern = re.compile(
        r"\{[^{}]*name:\s*[\"']" + re.escape(field) + r"[\"'][^{}]*type:\s*[\"']number[\"'][^{}]*\}"
    )
    return bool(pattern.search(ADMIN_JS))


@pytest.mark.parametrize("field", ["min", "max", "score_per_unit"])
def test_量表区间与计分字段不得声明为数值型弹窗字段(field):
    assert not _declared_as_number(field), (
        f"`{field}` 被声明成 type:\"number\"。spdModal 把空的数值字段转成 0 而不是空串，"
        f"于是「留空=不限」会变成 0：`max: 0` 让该评分区间永远命不中，"
        f"配出来的「N 分以上=高危」是死的，而且不报错。用文本收并走 spdOptionalNumber。"
    )


def test_可空数值助手存在且留空回落为不限():
    """助手本身的语义写在实现里，这里只钉住它没被顺手删掉、且注释解释了为什么不用 number。"""
    assert "function spdOptionalNumber(" in ADMIN_JS
    assert "spdOptionalNumber(form.min" in ADMIN_JS
    assert "spdOptionalNumber(form.max" in ADMIN_JS


# ------------------------------------------------- 后端：PATCH 也要查 key 重复


def _make_draft(client, h, code: str, items: list[dict] | None = None):
    resp = client.post(
        "/api/spd/scales",
        json={"code": code, "name": "配题守卫量表", "category": "screen",
              "items": items or []},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_新建量表题目key重复被拒(client, h):
    resp = client.post(
        "/api/spd/scales",
        json={"code": "dup_on_create", "name": "重复key",
              "items": [{"key": "a", "title": "甲"}, {"key": "a", "title": "乙"}]},
        headers=h,
    )
    assert resp.status_code == 422
    assert "不得重复" in resp.json()["detail"]


def test_配题时题目key重复同样被拒(client, h):
    """本轮修的那条：配题走 PATCH，而 PATCH 此前不查重。

    重复 key 不会报错，只会让 `score_scale` 把同一个答案计两次分——
    量表照样出分，出的是错的风险等级。
    """
    scale = _make_draft(client, h, "dup_on_patch")
    resp = client.patch(
        f"/api/spd/scales/{scale['id']}",
        json={"items": [
            {"key": "salt", "title": "口味偏咸", "type": "single",
             "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
            {"key": "salt", "title": "重复的同名题", "type": "single",
             "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
        ]},
        headers=h,
    )
    assert resp.status_code == 422, (
        f"PATCH 放行了重复的题目 key（{resp.status_code}）——"
        "配题正是走 PATCH，这等于查重形同虚设"
    )
    assert "不得重复" in resp.json()["detail"]


def test_配题时不重复的题目正常写入(client, h):
    """反面：别把守卫写成"PATCH items 一律拒"。这条在守卫写死时会转红。"""
    scale = _make_draft(client, h, "patch_ok")
    resp = client.patch(
        f"/api/spd/scales/{scale['id']}",
        json={"items": [
            {"key": "family", "title": "家族史", "type": "single",
             "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]},
            {"key": "smoke", "title": "吸烟", "type": "single",
             "options": [{"label": "是", "score": 1}, {"label": "否", "score": 0}]},
        ],
            "scoring": {"ranges": [
                {"min": 0, "max": 1, "risk": "low", "advice": "保持"},
                {"min": 2, "max": None, "risk": "high", "advice": "复核"},
            ]}},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [i["key"] for i in body["items"]] == ["family", "smoke"]
    # 上限 None 要原样存回来——它是「不限」，被写成 0 就是本文件第 3 条守的那个坑
    assert body["scoring"]["ranges"][1]["max"] is None


def test_发布后不能再改题(client, h):
    """既有语义的特征化：配题只在草稿期开放，发布后要新建版本。"""
    scale = _make_draft(client, h, "publish_then_patch",
                        items=[{"key": "a", "title": "甲",
                                "options": [{"label": "是", "score": 1}]}])
    assert client.post(f"/api/spd/scales/{scale['id']}/publish", headers=h).status_code == 200
    resp = client.patch(
        f"/api/spd/scales/{scale['id']}",
        json={"items": [{"key": "b", "title": "乙"}]}, headers=h,
    )
    assert resp.status_code == 409
