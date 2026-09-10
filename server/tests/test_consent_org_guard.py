"""知情同意书的签署/拒签必须校验机构归属（2026-09-10 实测取证后补）。

## 修的是什么

`POST /consents/{id}/sign` 与 `/refuse` 原先**连 `user` 形参都没有**。
实测取证（乙院医师，与该患者及该机构均无关）：

    POST /api/outpatient/consents/1/sign    → 200  签掉了甲院开的告知书
    POST /api/outpatient/consents/2/refuse  → 200  替甲院记了一笔"患方拒签"

知情同意书是**证据性法律文书**——机构靠它证明"告知过"。被别家机构签掉或记成
拒签，等于伪造了这份证据，而系统里看不出任何异常。

同一份文书上，**开具**（`create_consent`）一直校验机构
（`assert_org_writable`），**签署/拒签**却不校验——同一份文书上的两个动作
用两套口径，本身就是缺陷。已补齐为同一口径。

## 为什么闸门一直没报

与 `clinical_docs` 那族同一个盲区：横向越权闸门只看端点自身函数体里的
`db.get(…)`，而取行在本模块的 `_pending` helper 里。同一次改动已让闸门跟进
一层本模块调用（见 `test_stage15_horizontal.py`），这两个端点因此才现形。
"""
import pytest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def consent_world(client):
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "同意书甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "同意书乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org in (("ic_doc_a", a), ("ic_doc_b", b)):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": "doctor", "org_id": org["id"]},
                    headers=admin)
    patient = client.post("/api/patients",
                          json={"name": "同意书患者", "id_card": "320000199203034455"},
                          headers=admin).json()
    return {"admin": admin, "a": a, "b": b, "patient": patient,
            "doc_a": _login(client, "ic_doc_a"), "doc_b": _login(client, "ic_doc_b")}


def _new_consent(client, w, title="手术知情同意书"):
    r = client.post("/api/outpatient/consents",
                    json={"patient_id": w["patient"]["id"], "org_id": w["a"]["id"],
                          "consent_type": "surgery", "title": title, "content": "拟行手术…"},
                    headers=w["doc_a"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_本机构医师签署与拒签照常(client, consent_world):
    """先证明**放行的那条路是通的**——否则下面的 403 可能只是"全关了"。"""
    w = consent_world
    signed = client.post(f"/api/outpatient/consents/{_new_consent(client, w)}/sign",
                         json={"signer_name": "患者本人", "signer_relation": "self"},
                         headers=w["doc_a"])
    assert signed.status_code == 200, signed.text
    assert signed.json()["status"] == "signed"
    refused = client.post(f"/api/outpatient/consents/{_new_consent(client, w, '第二份')}/refuse",
                          json={"signer_name": "患者本人", "signer_relation": "self",
                                "refuse_reason": "要求保守治疗"},
                          headers=w["doc_a"])
    assert refused.status_code == 200, refused.text
    assert refused.json()["status"] == "refused"


def test_无关机构不得签署别家开的告知书(client, consent_world):
    w = consent_world
    r = client.post(f"/api/outpatient/consents/{_new_consent(client, w)}/sign",
                    json={"signer_name": "乙院代签", "signer_relation": "self"},
                    headers=w["doc_b"])
    assert r.status_code == 403, f"乙院签掉了甲院的告知书：{r.status_code} {r.text[:200]}"


def test_无关机构不得替别家记拒签(client, consent_world):
    """拒签是一等状态：机构据此证明"告知过、对方拒绝了"，伪造它与伪造签署同样严重。"""
    w = consent_world
    r = client.post(f"/api/outpatient/consents/{_new_consent(client, w)}/refuse",
                    json={"signer_name": "乙院代拒", "signer_relation": "self",
                          "refuse_reason": "乙院越权拒签"},
                    headers=w["doc_b"])
    assert r.status_code == 403, f"乙院替甲院记了拒签：{r.status_code} {r.text[:200]}"


def test_归属判定排在状态机之前(client, consent_world):
    """已处理过的告知书，对无关机构也要回 403 而不是 409。

    否则 409 的措辞（"该告知书已签署"）就把别家单据的状态探了出去——
    与 `billing.refund_payment` 同一理由。
    """
    w = consent_world
    cid = _new_consent(client, w, "已签过的")
    assert client.post(f"/api/outpatient/consents/{cid}/sign",
                       json={"signer_name": "患者本人", "signer_relation": "self"},
                       headers=w["doc_a"]).status_code == 200
    r = client.post(f"/api/outpatient/consents/{cid}/sign",
                    json={"signer_name": "乙院代签", "signer_relation": "self"},
                    headers=w["doc_b"])
    assert r.status_code == 403, (
        f"对无关机构应先 403，不该用 409 的措辞泄露状态：{r.status_code} {r.text[:200]}"
    )
