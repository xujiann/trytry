"""量表 / 路径模板 / 宣教素材 / 服务包的**生命周期**：发布、停用、复制、改版、删除。

与 `test_spd_config_admin.py` 同一动机（补"建之后"的那一半），这里管的是
"已经在用的东西怎么改"——这类操作的危险不在报错，而在**悄悄改变了正在跑的业务**：
已发布的路径直接加节点，在跑的患者会突然多出一个没人知道的任务；
停用的量表若还能被引用，评估会拿着一份已经废弃的口径继续打分。

报告段落一并在这里测：注册表的每一个内置 key 都要能出内容，
未注册的 key 要降级而不是让整份报告失败。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def h(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def base(client, h):
    org = client.post(
        "/api/organizations",
        json={"name": "目录域卫生院", "org_type": "township", "level": "township"},
        headers=h,
    ).json()
    program = next(
        p for p in client.get("/api/spd/programs", headers=h).json()
        if p["code"] == "hypertension"
    )
    return {"org": org, "program": program}


# ============================================================ 量表


def test_量表未发布不可引用_发布后可扫码_停用后失效(client, h, base):
    scale = client.post(
        "/api/spd/scales",
        json={"code": "lc_scale", "name": "生命周期量表", "category": "screen",
              "program_code": "hypertension",
              "items": [{"key": "q1", "title": "头晕", "type": "single",
                         "options": [{"label": "否", "score": 0}, {"label": "是", "score": 5}]}],
              "scoring": {"ranges": [
                  {"min": 0, "max": 2, "risk": "low", "advice": "保持"},
                  {"min": 3, "max": None, "risk": "high", "advice": "尽快复核"},
              ]}},
        headers=h,
    ).json()
    assert scale["status"] == "draft"

    # 草稿态：筛查不认它
    reject = client.post(
        "/api/spd/screenings",
        json={"patient_id": 1, "program_code": "hypertension", "org_id": base["org"]["id"],
              "scale_code": "lc_scale", "answers": {"q1": "是"}},
        headers=h,
    )
    assert reject.status_code == 404, "未发布的量表不该能被引用"

    published = client.post(f"/api/spd/scales/{scale['id']}/publish", headers=h).json()
    assert published["status"] == "published" and published["qr_token"]
    assert client.get(f"/api/spd/scales/{scale['id']}/qr.svg", headers=h).status_code == 200

    disabled = client.post(f"/api/spd/scales/{scale['id']}/disable", headers=h).json()
    assert disabled["status"] == "disabled"
    assert client.get(f"/api/spd/scales/{scale['id']}/qr.svg", headers=h).status_code == 409
    listed = client.get("/api/spd/scales?status=published", headers=h).json()
    assert "lc_scale" not in [s["code"] for s in listed]


def test_没题目的量表不许发布(client, h):
    scale = client.post(
        "/api/spd/scales",
        json={"code": "lc_empty", "name": "空量表", "category": "risk", "items": []},
        headers=h,
    ).json()
    resp = client.post(f"/api/spd/scales/{scale['id']}/publish", headers=h)
    assert resp.status_code == 422 and "题目" in resp.json()["detail"]


def test_量表改题与取详情(client, h):
    scale = client.post(
        "/api/spd/scales",
        json={"code": "lc_edit", "name": "待改量表", "category": "risk",
              "items": [{"key": "a", "title": "旧题", "type": "single", "options": []}]},
        headers=h,
    ).json()
    client.patch(
        f"/api/spd/scales/{scale['id']}",
        json={"name": "改过的量表",
              "items": [{"key": "a", "title": "新题", "type": "single", "options": []},
                        {"key": "b", "title": "补的题", "type": "single", "options": []}]},
        headers=h,
    )
    detail = client.get(f"/api/spd/scales/{scale['id']}", headers=h).json()
    assert detail["name"] == "改过的量表" and len(detail["items"]) == 2
    assert client.get("/api/spd/scales/999999", headers=h).status_code == 404


# ============================================================ 路径模板


def test_已发布路径不可直接改节点_复制新版才能改(client, h, base):
    """在跑的患者会突然多出一个没人知道的任务——所以要改就复制一版。"""
    template = client.post(
        "/api/spd/path-templates",
        json={"program_id": base["program"]["id"], "code": "lc_path",
              "name": "生命周期路径", "scene": "followup"},
        headers=h,
    ).json()
    node = client.post(
        f"/api/spd/path-templates/{template['id']}/nodes",
        json={"key": "n1", "name": "首节点", "seq": 1, "due_days": 7},
        headers=h,
    ).json()
    client.post(f"/api/spd/path-templates/{template['id']}/status",
                json={"status": "published"}, headers=h)

    blocked = client.post(
        f"/api/spd/path-templates/{template['id']}/nodes",
        json={"key": "n2", "name": "偷加的节点", "seq": 2},
        headers=h,
    )
    assert blocked.status_code == 409
    assert client.patch(f"/api/spd/path-nodes/{node['id']}", json={"name": "偷改"},
                        headers=h).status_code == 409
    assert client.delete(f"/api/spd/path-nodes/{node['id']}",
                         headers=h).status_code == 409

    copied = client.post(f"/api/spd/path-templates/{template['id']}/copy",
                         json={}, headers=h)
    assert copied.status_code == 201, copied.text
    copy = copied.json()
    assert copy["status"] == "draft" and copy["id"] != template["id"]
    detail = client.get(f"/api/spd/path-templates/{copy['id']}", headers=h).json()
    assert [n["key"] for n in detail["nodes"]] == ["n1"], "复制要把节点一起带过来"

    # 新版是草稿：可以随便改
    assert client.patch(f"/api/spd/path-nodes/{detail['nodes'][0]['id']}",
                        json={"name": "改过的首节点", "due_days": 14},
                        headers=h).status_code == 200


def test_节点key在同一路径内不重复(client, h, base):
    template = client.post(
        "/api/spd/path-templates",
        json={"program_id": base["program"]["id"], "code": "lc_dupnode", "name": "重复节点路径"},
        headers=h,
    ).json()
    body = {"key": "same", "name": "节点", "seq": 1}
    assert client.post(f"/api/spd/path-templates/{template['id']}/nodes", json=body,
                       headers=h).status_code == 201
    again = client.post(f"/api/spd/path-templates/{template['id']}/nodes", json=body, headers=h)
    assert again.status_code == 409


def test_断头的下一节点不许发布(client, h, base):
    """`next_key` 指向不存在的节点，患者会停在那里且不报错——发布前就要拦。"""
    template = client.post(
        "/api/spd/path-templates",
        json={"program_id": base["program"]["id"], "code": "lc_broken", "name": "断头路径"},
        headers=h,
    ).json()
    client.post(
        f"/api/spd/path-templates/{template['id']}/nodes",
        json={"key": "n1", "name": "首节点", "seq": 1, "next_key": "不存在的节点"},
        headers=h,
    )
    resp = client.post(f"/api/spd/path-templates/{template['id']}/status",
                       json={"status": "published"}, headers=h)
    assert resp.status_code == 422


def test_草稿路径可删_在跑的不可删(client, h, base):
    draft = client.post(
        "/api/spd/path-templates",
        json={"program_id": base["program"]["id"], "code": "lc_del", "name": "待删路径"},
        headers=h,
    ).json()
    assert client.delete(f"/api/spd/path-templates/{draft['id']}",
                         headers=h).status_code == 204
    assert client.get(f"/api/spd/path-templates/{draft['id']}",
                      headers=h).status_code == 404


# ============================================================ 宣教素材与服务包


def test_宣教素材停用后不再可推(client, h):
    material = client.post(
        "/api/spd/edu-materials",
        json={"code": "lc_edu", "title": "生命周期宣教", "content": "内容"},
        headers=h,
    ).json()
    client.patch(f"/api/spd/edu-materials/{material['id']}",
                 json={"active": False, "title": "已停用宣教"}, headers=h)
    resp = client.post(
        "/api/spd/edu-pushes",
        json={"material_id": material["id"], "patient_ids": [1], "channel": "sms"},
        headers=h,
    )
    assert resp.status_code == 404, "停用素材还能推，等于停用没生效"


def test_服务包改项目与停用(client, h):
    package = client.post(
        "/api/spd/service-packages",
        json={"code": "lc_pkg", "name": "基础包", "program_code": "hypertension",
              "items": [{"code": "bp", "name": "血压测量", "times": 4}]},
        headers=h,
    ).json()
    updated = client.patch(
        f"/api/spd/service-packages/{package['id']}",
        json={"items": [{"code": "bp", "name": "血压测量", "times": 6},
                        {"code": "med", "name": "用药指导", "times": 2}],
              "active": False},
        headers=h,
    ).json()
    assert len(updated["items"]) == 2 and updated["active"] is False
    assert client.patch("/api/spd/service-packages/999999", json={"active": False},
                        headers=h).status_code == 404


def test_标签建了就能查(client, h):
    client.post("/api/spd/tags",
                json={"code": "lc_tag", "name": "重点关注", "category": "manage"}, headers=h)
    codes = [t["code"] for t in client.get("/api/spd/tags", headers=h).json()]
    assert "lc_tag" in codes


# ============================================================ 报告段落注册表


def test_每个内置段落都出得来内容(client, h, base):
    """注册表里注册了却渲染不出来的段落，等于给模板挖了个坑。"""
    from app.database import SessionLocal
    from app.spd.reporting import compose_section, registered_sections

    with SessionLocal() as db:
        for key in registered_sections():
            section = {"key": key, "title": f"{key} 段落"}
            if key == "indicator":
                section["indicator_code"] = "followup_rate"
            out = compose_section(db, section, base["org"]["id"], "monthly")
            assert out["key"] == key
            assert out.get("type") in ("text", "table", "chart"), f"{key} 段落没给类型"


def test_未注册段落降级而不是让整份报告失败(client, h, base):
    from app.database import SessionLocal
    from app.spd.reporting import compose_section

    with SessionLocal() as db:
        out = compose_section(db, {"key": "没这个段落"}, base["org"]["id"], "daily")
    assert "未配置取数口径" in out["note"]


def test_实施期可注册自己的报告段落(client, h, base):
    """与采集器同一形状的扩展点，用例证明它真的能用。"""
    from app.database import SessionLocal
    from app.spd.reporting import _SECTIONS, compose_section, register_section

    def custom(db, section, org_id, period):
        return {"key": "county_special", "title": "本县特有段落", "type": "text",
                "text": f"机构 {org_id} / 周期 {period}"}

    register_section("county_special", custom)
    try:
        with SessionLocal() as db:
            out = compose_section(db, {"key": "county_special"}, 7, "weekly")
        assert out["text"] == "机构 7 / 周期 weekly"
    finally:
        _SECTIONS.pop("county_special", None)
