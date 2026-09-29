"""签约建档表单与「调整」弹窗补危险因素 / 并发症 / 知情同意（P2-858，第二十三批「页面表单提交的字段与取值 vs 后端请求模型」
扫描 Y1-7）。

`EnrollIn` / `EnrollUpdate` 早就收这几项，档案的 `archived`（「已建档 / 待完善」）= 生活习惯、危险因素、并发症任一有值，考核
种子指标「建档完整率」按它算；`create_enrollment` 的 docstring 写「先建档、后补签是常态」。全仓前端没有一处写这几个字段：
界面建的档案 archived 恒为 false、清单全是「待完善」、建档完整率恒 0，「后补签」在界面上做不到。修后两处都补上
（逗号分隔转数组）；生活习惯 habits 是字典，收哪些键另行待裁定。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def test_建档表单有危险因素并发症知情同意():
    start = PAGE.index('<form class="inline" id="spd-enroll-form">')
    form = PAGE[start:PAGE.index("</form>", start)]
    for field in ('<input name="risk_factors"', '<input name="complications"', 'name="consent_signed"',
                  '<input name="consent_no"'):
        assert field in form, field   # 修前一项都没有
    start = PAGE.index('$("#spd-enroll-form").onsubmit')
    submit = PAGE[start:PAGE.index("};", start)]
    assert "body[k] = spdSplitList(body[k])" in submit and "body.consent_signed = e.target.consent_signed.checked;" in submit


def test_调整弹窗能补签():
    start = PAGE.index('spdModal("调整纳管档案（留空的项不改）"')
    body = PAGE[start:PAGE.index("/api/spd/enrollments/${enrEdit.dataset.enrEdit}", start)]
    for name in ("risk_factors", "complications", "consent_signed", "consent_no"):
        assert f'{{ name: "{name}"' in body, name
    assert 'body.consent_signed = form.consent_signed === "true";' in body


def test_按页面送的危险因素建档_算已建档(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2858 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2858 患者", "id_card": "330102196001012858"}).json()["id"]
    made = client.post("/api/spd/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org,
        "risk_factors": ["吸烟", "肥胖"], "consent_signed": True, "consent_no": "TY-2858"})
    assert made.status_code == 201, made.text
    assert (made.json()["archived"], made.json()["risk_factors"]) == (True, ["吸烟", "肥胖"]), made.json()
