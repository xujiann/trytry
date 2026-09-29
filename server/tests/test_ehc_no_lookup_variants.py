"""手输的健康卡号写成小写、带首尾空白也认：档案详情与 360 视图原先按原样等值比，一律 404（P2-791，第二十一批「同一个值的
不同写法」扫描 N3-10）。

卡号由系统生成（`EHC` + 大写十六进制），关键字检索（模糊、不分大小写）搜得到的同一个人，按卡号取档却 404。修后原样命中
的照旧，取不到再按去首尾空白、大写找一次；对接方按引用取档（FHIR）与就诊凭据的卡号识别不走这里。
"""
import pytest


@pytest.fixture(scope="module")
def patient(client, admin):
    resp = client.post("/api/patients", headers=admin, json={"name": "P2791 患者", "id_card": "330102196505052791"})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize("variant", [str.lower, lambda ehc: f" {ehc} ", lambda ehc: ehc[:3] + ehc[3:].lower()],
                         ids=["全小写", "首尾空格", "数字段小写"])
def test_写法不同的卡号_档案详情与360视图都取得到(client, admin, patient, variant):
    query = variant(patient["ehc_no"])
    assert query != patient["ehc_no"]   # 判据自证：确实换了写法
    detail = client.get(f"/api/patients/{query}", headers=admin)
    assert detail.status_code == 200 and detail.json()["id"] == patient["id"], detail.text   # 修前 404
    archive = client.get(f"/api/archive/{query}", headers=admin)
    assert archive.status_code == 200 and archive.json()["patient"]["ehc_no"] == patient["ehc_no"], archive.text


def test_原样卡号照旧_查无此号照旧404(client, admin, patient):
    assert client.get(f"/api/patients/{patient['ehc_no']}", headers=admin).json()["id"] == patient["id"]
    assert client.get("/api/patients/EHC000000000000", headers=admin).status_code == 404
    assert client.get("/api/archive/ehc000000000000", headers=admin).status_code == 404
