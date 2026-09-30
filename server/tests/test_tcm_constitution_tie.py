"""中医体质辨识：最高转化分并列时，「判定体质」取请求里先出现的那个键（P2-966，第二十七批「排名、Top-N、并列与空值」扫描
G4-6 的判定体质那一半）。

`constitution_identify` 用 `max(valid.items(), key=分)`，取第一个最大值——`valid` 保留请求里的键序；同一个响应里的兼夹体质按
（分降序, 编码）排。气虚 50、阳虚 50：先写气虚判气虚质、方剂补中益气汤；只换一下键序就判阳虚质、金匮肾气丸。网页按表单的
固定次序拼 `scores`，等于表单里排在前面的体质胜出。修法：判定体质与兼夹体质同一个次序，结果与键序无关；并列的另一种照旧
列进兼夹体质。辨证、导诊的前 3 截在并列处另行登记。
"""
B = "/api/tcm"


def test_最高分并列_判定体质与键序无关_另一种进兼夹(client, admin):
    one = client.post(f"{B}/constitution", headers=admin,
                      json={"scores": {"qi_deficiency": 50, "yang_deficiency": 50}}).json()
    other = client.post(f"{B}/constitution", headers=admin,
                        json={"scores": {"yang_deficiency": 50, "qi_deficiency": 50}}).json()
    assert (one["constitution"], one["formula"]) == (other["constitution"], other["formula"])   # 修前随键序变
    assert one["constitution"] == "气虚质" and one["also"] == ["阳虚质"]
