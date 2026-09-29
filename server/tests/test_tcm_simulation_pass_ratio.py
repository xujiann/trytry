"""模拟病例判及格用没取整的比例，展示分四舍五入逢 .5 进一（P2-893，第二十四批「阈值与边界值」扫描 Z4-6）。

`submit_simulation` 先 `score = round(earned*100/total)` 再 `passed = score >= pass_score`：三题分值 60/59/81，答对前两题
119/200 = 59.5%，取整成 60、判及格；Python 的 round 逢 .5 取偶，60.5 显示 60、61.5 显示 62。页面写「及格分按百分制：得分 =
答对的分值 ÷ 满分 × 100」，没说要取整。修后判及格用 `earned*100 >= pass_score*total`；展示分常规四舍五入。
"""
import pytest

B = "/api/tcm-heritage/simulations"


def _case(client, admin, scores):
    points = [{"key": f"s{i}", "question": f"第{i}问", "options": ["对", "错"], "answer": "对", "score": score}
              for i, score in enumerate(scores)]
    made = client.post(B, headers=admin, json={"title": f"P2893 {scores}", "decision_points": points, "pass_score": 60})
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _submit(client, admin, case, right):
    got = client.post(f"{B}/{case}/attempts", headers=admin, json={"answers": {f"s{i}": "对" for i in right}})
    assert got.status_code == 201, got.text
    return got.json()["score"], got.json()["passed"]


@pytest.mark.parametrize(("scores", "right", "expected"), [
    ((60, 59, 81), (0, 1), (60, False)),   # 119/200 = 59.5%：修前 (60, True)
    ((60, 60, 80), (0, 1), (60, True)),    # 120/200 = 60%：照旧及格
    ((61, 60, 79), (0, 1), (61, True)),    # 121/200 = 60.5%：修前显示 60（逢 .5 取偶）
    ((62, 61, 77), (0, 1), (62, True)),    # 123/200 = 61.5%：逢 .5 进一
], ids=["59.5不及格", "60及格", "60.5显示61", "61.5显示62"])
def test_判及格看比例_展示分四舍五入(client, admin, scores, right, expected):
    assert _submit(client, admin, _case(client, admin, scores), right) == expected
