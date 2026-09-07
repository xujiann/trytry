"""P2-8 第四批 4b：8 个**有收口或按设计全域可见**的端点切 `deps.paginate`。

与 4a（修四处「统计吃截断样本」的缺陷）分开提交：一个是修 bug，一个是治理，
混在一个提交里 review 分不开。切法与前三批一致——`limit` 默认值取原硬编码值，
所以不带参数调用时第一页逐条不变；既有 81 条相关用例一条没改就全绿。

**这一批把「有没有收口」这条判据自己校准了一次。** 第四批审计最初把
`spd:consult_messages` 判成「无收口」，因为扫描只认 `scope_org_list` /
`scope_patient_list` / `visible_org_ids` 这几个名字；实际它用的是
`assert_patient_visible(db, user, consult.patient_id, ...)`——**是收口，只是名字不在表里**。
同一处偏差也让仓库级的统计报小了「有收口」的那一半（21/104 → 实为 26/97）。
**判据比缺陷窄**这条教训，这一轮在我自己的度量上又发生了一次。

三张配置目录表（干预模板 / 问卷 / 报表模板）**没有收口是对的、不是缺口**：
它们是按设计全域可见的配置字典，与量表目录同类。把这类和「业务数据缺收口」
分开数，才谈得上裁定 P1-49。
"""
import pytest

MIGRATED = [
    ("/api/maternal/women-health", {}),
    ("/api/vaccine-supply/batches", {}),
    ("/api/vaccine-supply/cold-chain", {}),
    ("/api/vaccine-supply/aefi", {}),
    ("/api/spd/intervention-templates", {}),
    ("/api/spd/questionnaires", {}),
    ("/api/spd/report-templates", {}),
]


@pytest.mark.parametrize("path,params", MIGRATED)
def test_切过的端点都带上了总数头(client, admin, path, params):
    resp = client.get(path, headers=admin, params=params)
    assert resp.status_code == 200, resp.text
    assert "X-Total-Count" in resp.headers, f"{path} 没带 X-Total-Count"


def test_翻页不重不漏(client, admin):
    """拿问卷目录翻——它是配置表，条数稳定、不受别的用例影响。"""
    for i in range(5):
        client.post("/api/spd/questionnaires", headers=admin,
                    json={"code": f"PGQ-{i}", "name": f"分页问卷{i}", "scene": "followup",
                          "items": [{"key": "q1", "label": "题", "type": "single",
                                     "options": ["A", "B"]}]})
    total = int(client.get("/api/spd/questionnaires", headers=admin)
                .headers["X-Total-Count"])
    assert total >= 5
    seen, offset = [], 0
    while offset < total:
        rows = client.get("/api/spd/questionnaires", headers=admin,
                          params={"offset": offset, "limit": 2}).json()
        assert rows, "翻页翻到空页说明 offset 没生效"
        seen.extend(r["id"] for r in rows)
        offset += 2
    assert len(seen) == total == len(set(seen)), "翻页结果有重复或缺漏"


def test_疫苗批次的可用过滤下推之后头与体数同一批行(client, admin):
    """`usable_only` 原来是取完 500 行再用 Python 丢行。

    留在 `paginate` 外面，`X-Total-Count`（含不可用的）与响应体（只剩可用的）
    就会对不上——调用方按「`len(page) < limit` 即最后一页」翻页会在第一页早停，
    **把静默截断换成了静默早停**。下推之后两者数的是同一批行。
    """
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "4b 疫苗院", "org_type": "lead_hospital",
                            "level": "county"}).json()
    for i, (expire, used) in enumerate(
        [("2099-01-01", 0), ("2099-01-01", 0), ("2020-01-01", 0), ("2099-01-01", 100)]
    ):
        client.post("/api/vaccine-supply/batches", headers=admin,
                    json={"org_id": org["id"], "vaccine_code": "V4B",
                          "vaccine_name": "4b 苗", "batch_no": f"B4B-{i}",
                          "manufacturer": "厂", "expire_date": expire, "quantity": 100})
    resp = client.get("/api/vaccine-supply/batches", headers=admin,
                      params={"org_id": org["id"], "vaccine_code": "V4B",
                              "usable_only": True})
    rows = resp.json()
    # 4 条里：2 条可用、1 条已过期、1 条尚未耗尽（入库即 used=0，故仍可用）
    assert all(r["usable"] for r in rows), "过滤没生效"
    assert int(resp.headers["X-Total-Count"]) == len(rows), (
        "总数头把不可用的也算进去了——头与体数的不是同一批行"
    )
    assert not [r for r in rows if r["expire_date"] == "2020-01-01"]


def test_咨询消息按患者可见性收口而不是无收口(client, admin):
    """审计一度把它判成「无收口」，因为扫描不认 `assert_patient_visible` 这个名字。

    这条钉的是**结论**而不是名字：换个不该看的人来调，必须被拦住。
    """
    import inspect

    from app.spd.routers import care

    src = inspect.getsource(care.consult_messages)
    assert "assert_patient_visible" in src, (
        "这个端点的收口没了——它是按患者可见性收口的，不是靠 scope_* 那几个名字"
    )
    assert "paginate(" in src
