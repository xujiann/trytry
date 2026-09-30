"""课件「点播排行（前 20）」只按点播量排、没有尾键：并列的谁在前、零次的列哪几个由库决定（P2-965，第二十七批「排名、Top-N、
并列与空值」扫描 G4-5）。

`material_stats` 原先 `order_by(play_count.desc()).limit(20)`：25 个课件只有 4 个被点过，榜单 20 行里 16 行是 0 次点播，21 个
0 次的里列哪 16 个由库的返回次序决定（每点播一次就 UPDATE 一次，PG 上这个次序跟着变）。同类榜单（用药地图、互认项目、
积分榜）都补了尾键。修法：点播量并列按课件编号；零次的照旧列在后面（按编号），接口内容不变。
"""
import pytest

B = "/api/education"


@pytest.fixture(scope="module")
def materials(client, admin):
    course = client.post(f"{B}/courses", headers=admin, json={"title": "P2965 课程"})
    assert course.status_code == 201, course.text
    ids = []
    for n in range(4):
        made = client.post(f"{B}/courses/{course.json()['id']}/materials", headers=admin,
                           json={"title": f"P2965 课件{n}"})
        assert made.status_code == 201, made.text
        ids.append(made.json()["id"])
    for mid, plays in zip(ids, (1, 2, 2, 0)):
        for _ in range(plays):
            assert client.post(f"{B}/materials/{mid}/play", headers=admin).status_code == 200
    return ids


def test_点播量并列按课件编号_零次的按编号排在后面(client, admin, materials):
    top = client.get(f"{B}/material-stats", headers=admin).json()["top"]
    mine = [(m["id"], m["play_count"]) for m in top if m["title"].startswith("P2965")]
    assert mine == [(materials[1], 2), (materials[2], 2), (materials[0], 1), (materials[3], 0)]


def test_排行查询带课件编号尾键():
    import ast
    import inspect
    import textwrap

    from app.routers import education

    tree = ast.parse(textwrap.dedent(inspect.getsource(education.material_stats)))
    orders = [ast.unparse(n) for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "order_by"]
    assert orders and all(o.endswith("order_by(CourseMaterial.play_count.desc(), CourseMaterial.id)") for o in orders), orders
