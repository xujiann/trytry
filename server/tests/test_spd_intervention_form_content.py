"""批量下发干预表单补上内容 / 措施 / 频次：原先缺省「不引用模板」一提交就 422，新部署一个模板都没有时界面发不出任何干预
（P2-855，第二十三批「页面表单提交的字段与取值 vs 后端请求模型」扫描 Y1-4）。

`InterventionIn` 早就收 content / measures / frequency（留空时取模板的），没模板又没内容 422「干预内容不能为空」；成员端 #15
写的是「设置内容与下次干预时间」。页面的「批量下发」表单只有患者、病种、模板、目标、下次时间——模板下拉缺省「不引用模板」，
不改就提交必 422；新部署还没建模板时，界面上发不出任何干预。修后表单补这三项，留空的不送（后端取模板的）。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def test_页面表单有内容措施频次三项():
    start = PAGE.index('<form class="inline" id="spd-intv-form"')
    form = PAGE[start:PAGE.index("</form>", start)]
    for name in ("content", "measures", "frequency"):
        assert f'<input name="{name}"' in form, name   # 修前一项都没有


def test_不引用模板_按页面送的内容照发(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2855 患者", "id_card": "330102196001012855"}).json()["id"]
    made = client.post("/api/spd/interventions", headers=admin, json={
        "patient_ids": [patient], "content": "低盐饮食，每日食盐不超过 5 克", "measures": "家属监督", "frequency": "每日",
        "create_task": False})   # 页面 formJson 的样子：模板留空不送
    assert made.status_code == 201, made.text
    row = next(r for r in client.get("/api/spd/interventions", headers=admin, params={"patient_id": patient}).json())
    assert (row["content"], row["measures"], row["frequency"]) == ("低盐饮食，每日食盐不超过 5 克", "家属监督", "每日")
