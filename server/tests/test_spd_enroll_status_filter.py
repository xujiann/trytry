"""「签约建档纳管」清单能按状态查：已死亡、召回中的档案按姓名查得到（P2-823，第二十二批「页面查询参数 vs 后端」扫描 X4-1）。

`list_enrollments` 的 `status` 缺省是 `active`，页面的筛选栏只有病种、风险、关键字三栏、从不带 status——已登记死亡或召回的
人，按姓名查回来是空表，页面看起来就是「此人没建过档」，操作员再签一次后端照收（同一病种死亡后再签约照收属 P1-112 的
口径，另并入）。状态列本来就写了非在管状态的显示分支。修后筛选栏加状态（缺省在管，「全部状态」送 `status=`，后端空串即
不筛），按姓名在「在管」里查空了提示去「全部状态」里找。
"""
from pathlib import Path

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def test_已死亡的档案_缺省查不到_带状态查得到(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2823 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2823已故王某", "id_card": "330106195001012823"}).json()["id"]
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert enrolled.status_code == 201, enrolled.text
    died = client.post(f"{B}/enrollments/{enrolled.json()['id']}/lifecycle", headers=admin,
                       json={"event": "death", "reason": "病故"})
    assert died.status_code == 200, died.text

    def ids(**params):
        rows = client.get(f"{B}/enrollments", headers=admin, params={"limit": 30, "keyword": "P2823已故王某", **params})
        assert rows.status_code == 200, rows.text
        return [(r["id"], r["status"]) for r in rows.json()]

    assert ids() == []   # 页面原先就是这么查的：空表
    assert ids(status="") == [(enrolled.json()["id"], "dead")]   # 「全部状态」
    assert ids(status="dead") == [(enrolled.json()["id"], "dead")]


def test_筛选栏有状态_全部状态送空串():
    start = PAGE.index('<form class="inline" id="spd-enroll-filter">')
    form = PAGE[start:PAGE.index("</form>", start)]
    assert '<select name="status"><option value="active">在管</option><option value="all">全部状态</option>' in form
    assert 'Object.entries(SPD_ENROLL_STATUS).filter(([v]) => v !== "active")' in form   # 其余状态照状态列的文案
    start = PAGE.index("const drawEnrollments = async (query) => {")
    draw = PAGE[start:PAGE.index("\n  };\n", start)]
    assert 'if (q.status === "all") q.status = "";' in draw   # 修前从不带 status
    assert "请在状态里选「全部状态」再查" in draw
