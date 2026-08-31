"""深化轮（6-7）：急救绿道时间节点、中医知识库扩充与标准化简表计分。"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "绿道县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    resp = client.post(
        "/api/users",
        json={"username": "gt_pharm", "password": "pass123456", "role": "pharmacist", "org_id": org["id"]},
        headers=admin,
    )
    assert resp.status_code == 201
    return {"org": org, "pharmacist": login(client, "gt_pharm", "pass123456")}


# ---------------------------------------------------------------- 6 急救绿道


def test_channel_type_on_case(client, admin, setup):
    case = client.post(
        "/api/emergency/cases",
        json={"location": "西镇广场", "symptom": "胸痛大汗", "channel_type": "chest_pain",
              "dest_org_id": setup["org"]["id"]},
        headers=admin,
    )
    assert case.status_code == 201
    assert case.json()["channel_type"] == "chest_pain"
    # 非法通道类型被拒绝
    bad = client.post(
        "/api/emergency/cases",
        json={"location": "西镇广场", "channel_type": "unknown"},
        headers=admin,
    )
    assert bad.status_code == 422


def test_milestones_and_timeline(client, admin, setup):
    case = client.post(
        "/api/emergency/cases",
        json={"location": "东村委", "symptom": "言语不清偏瘫", "channel_type": "stroke"},
        headers=admin,
    ).json()
    cid = case["id"]

    steps = [
        ("onset", "2026-08-10 13:50"),
        ("call", "2026-08-10 14:02"),
        ("depart", "2026-08-10 14:05"),
        ("arrive_scene", "2026-08-10 14:18"),
        ("arrive_hospital", "2026-08-10 14:40"),
    ]
    for milestone, occurred_at in steps:
        resp = client.post(
            f"/api/emergency/cases/{cid}/milestones",
            json={"milestone": milestone, "occurred_at": occurred_at},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text

    # 同一节点重复记录 → 409；非法节点名 → 422；药师无权记录 → 403
    dup = client.post(
        f"/api/emergency/cases/{cid}/milestones",
        json={"milestone": "call", "occurred_at": "2026-08-10 14:03"},
        headers=admin,
    )
    assert dup.status_code == 409
    assert (
        client.post(
            f"/api/emergency/cases/{cid}/milestones",
            json={"milestone": "door_to_balloon", "occurred_at": "2026-08-10 15:00"},
            headers=admin,
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/emergency/cases/{cid}/milestones",
            json={"milestone": "treatment", "occurred_at": "2026-08-10 14:55"},
            headers=setup["pharmacist"],
        ).status_code
        == 403
    )

    timeline = client.get(f"/api/emergency/cases/{cid}/timeline", headers=admin).json()
    assert timeline["channel_type"] == "stroke"
    assert timeline["recorded_count"] == 5
    keys = [t["milestone"] for t in timeline["timeline"]]
    assert keys == ["onset", "call", "depart", "arrive_scene", "arrive_hospital", "treatment"]
    by_key = {t["milestone"]: t for t in timeline["timeline"]}
    assert by_key["call"]["occurred_at"] == "2026-08-10 14:02"
    assert by_key["treatment"]["recorded"] is False and by_key["treatment"]["occurred_at"] is None

    # 补记救治节点后时间轴完整
    client.post(
        f"/api/emergency/cases/{cid}/milestones",
        json={"milestone": "treatment", "occurred_at": "2026-08-10 14:52"},
        headers=admin,
    )
    full = client.get(f"/api/emergency/cases/{cid}/timeline", headers=admin).json()
    assert full["recorded_count"] == 6
    assert all(t["recorded"] for t in full["timeline"])

    assert client.get("/api/emergency/cases/99999/timeline", headers=admin).status_code == 404


# ---------------------------------------------------------------- 7 中医知识库扩充


def test_constitutions_cover_all_nine(client, admin):
    from app.routers.tcm import CONSTITUTIONS

    names = {v["name"] for v in CONSTITUTIONS.values()}
    assert names == {
        "平和质", "气虚质", "阳虚质", "阴虚质", "痰湿质",
        "湿热质", "血瘀质", "气郁质", "特禀质",
    }
    # 新增体质可被判定
    result = client.post(
        "/api/tcm/constitution", json={"scores": {"damp_heat": 70, "blood_stasis": 20}}, headers=admin
    ).json()
    assert result["constitution"] == "湿热质"
    stasis = client.post(
        "/api/tcm/constitution", json={"scores": {"blood_stasis": 55}}, headers=admin
    ).json()
    assert stasis["constitution"] == "血瘀质" and "血府逐瘀汤" in stasis["formula"]


def test_syndrome_kb_expanded(client, admin):
    from app.routers.tcm import SYNDROME_KB

    assert len(SYNDROME_KB) >= 12
    syndromes = {kb["syndrome"] for kb in SYNDROME_KB}
    assert {"感冒风寒证", "感冒风热证", "脾胃虚寒证", "肝郁气滞证", "血瘀证", "湿热证"} <= syndromes

    cold = client.post(
        "/api/tcm/assist-diagnosis",
        json={"symptoms": ["恶寒重", "无汗", "流清涕"]},
        headers=admin,
    ).json()
    assert cold["recommendations"][0]["syndrome"] == "感冒风寒证"
    liver = client.post(
        "/api/tcm/assist-diagnosis",
        json={"symptoms": ["胁肋胀痛", "善太息", "情志抑郁"]},
        headers=admin,
    ).json()
    assert liver["recommendations"][0]["syndrome"] == "肝郁气滞证"
    assert liver["recommendations"][0]["formula"] == "柴胡疏肝散"


def test_constitution_standard_form_scoring(client, admin):
    # 简表计分说明
    spec = client.get("/api/tcm/constitution/spec", headers=admin).json()
    assert "转化分" in spec["transformed_score"]
    assert len(spec["constitutions"]) == 9

    # 逐条计分：气郁质 7 条全 4 分 → 转化分 (28-7)/(7*4)*100 = 75
    answers = {
        "qi_stagnation": [4, 4, 4, 4, 4, 4, 4],
        "qi_deficiency": [2, 2, 1, 1, 2, 1, 2, 1],  # 原始12，条目8 → (12-8)/32*100 = 12.5 ≈ 12
    }
    result = client.post("/api/tcm/constitution", json={"answers": answers}, headers=admin).json()
    assert result["constitution"] == "气郁质"
    assert result["transformed_scores"]["qi_stagnation"] == 75
    assert result["transformed_scores"]["qi_deficiency"] == 12

    # 全部低分 → 平和质；倾向体质（30-39）单独提示
    mild = client.post(
        "/api/tcm/constitution",
        json={"answers": {"damp_heat": [2, 2, 2, 3, 2, 2], "yin_deficiency": [1, 1, 1, 1]}},
        headers=admin,
    ).json()
    # damp_heat 原始13条目6 → (13-6)/24*100 = 29.17 ≈ 29 → 平和质
    assert mild["constitution"] == "平和质"

    tendency = client.post(
        "/api/tcm/constitution",
        json={"scores": {"yang_deficiency": 55, "phlegm_damp": 35}},
        headers=admin,
    ).json()
    assert tendency["constitution"] == "阳虚质"
    assert tendency["tendencies"] == ["痰湿质"]

    # 非法输入
    assert client.post("/api/tcm/constitution", json={}, headers=admin).status_code == 422
    assert (
        client.post(
            "/api/tcm/constitution", json={"answers": {"damp_heat": [0, 6]}}, headers=admin
        ).status_code
        == 422
    )
