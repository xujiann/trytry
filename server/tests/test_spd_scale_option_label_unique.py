"""量表同一题里两个同名选项，建量表、发布都照收，评分取后写的分值（P2-1275，第三十七批「一次请求、一次导入里的重复元素」
扫描 AA2-6）。

`spd/rules.py::score_scale` 按 `{标签: 分值}` 查分，同名的后写的盖掉先写的；建 / 改 / 发布共用的 `config/scales.py::
_check_scale`（`scale_problem` 等）不查同名。修前实测：选项写成「是=3 / 否=0 / 是=0」（复制上一题选项后漏改），建量表 201、
发布 200，筛查答「是」（本该 3 分高危）得 0 分、判低危、未见异常。同文件 `_check_item_keys` 的 docstring 自己写着「题目 key
重复，评分时后一题会盖掉前一题」，选项是同一种覆盖却没拦。

修法：`rules.scale_option_label_problem` 进 `_check_scale`，建、改、发布都 422 并点名题目与标签；只在配置的写入口拦、不进作答时
也查的 `scale_problem`，存量已发布的量表照常作答、评分行为不改（同名的照旧取后写的分值），出新版本时改。
"""
import pytest

from app.spd.rules import scale_option_label_problem
from app.spd.seed import SEED_SCALES

B = "/api/spd"
DUP_ITEMS = [{"key": "q1", "title": "一级亲属有糖尿病", "type": "single",
              "options": [{"label": "是", "score": 3}, {"label": "否", "score": 0}, {"label": "是", "score": 0}]},
             {"key": "q2", "title": "BMI≥24", "type": "single",
              "options": [{"label": "是", "score": 2}, {"label": "否", "score": 0}]}]
GOOD_ITEMS = [dict(DUP_ITEMS[0], options=DUP_ITEMS[0]["options"][:2]), DUP_ITEMS[1]]   # 两道题各有「是 / 否」，互不相干
SCORING = {"ranges": [{"min": 0, "max": 2, "risk": "low", "advice": "低危"},
                      {"min": 3, "max": 100, "risk": "high", "advice": "高危，建议查空腹血糖"}]}
DETAIL = ("量表配置非法：题目 q1 的选项标签重复：「是」（评分按标签取分值，后写的会盖掉先写的，请删掉多余的一条或改名）")


def test_同一题选项同名才算_按评分同一个读法比():
    assert scale_option_label_problem(DUP_ITEMS) == DETAIL.removeprefix("量表配置非法：")
    assert scale_option_label_problem(GOOD_ITEMS) == ""
    multi = [{"key": "sym", "type": "multi", "options": [{"label": s, "score": 1} for s in ("头晕", "胸闷", "头晕", "胸闷")]}]
    assert scale_option_label_problem(multi).startswith("题目 sym 的选项标签重复：「头晕」、「胸闷」")
    as_number = [{"key": "q9", "options": [{"label": 1, "score": 1}, {"label": "1", "score": 2}]}]   # score_scale 都按 "1" 查
    assert scale_option_label_problem(as_number).startswith("题目 q9 的选项标签重复：「1」")
    assert scale_option_label_problem([{"key": "q8", "options": [{"score": 1}, {"score": 2}]}]) == ""   # 没写标签的不算同名


def test_种子量表没有同名选项():
    assert [s["code"] for s in SEED_SCALES if scale_option_label_problem(s["items"])] == []


def _scale(client, admin, code, items):
    return client.post(f"{B}/scales", headers=admin, json={
        "code": code, "name": f"{code} 量表", "category": "screen", "program_code": "diabetes",
        "items": items, "scoring": SCORING})


def test_建量表_同一题选项同名_422点名题目与标签(client, admin):
    resp = _scale(client, admin, "P21275_NEW", DUP_ITEMS)
    assert (resp.status_code, resp.json()) == (422, {"detail": DETAIL}), resp.text   # 修前 201


def test_改草稿_同一题选项同名_422(client, admin):
    created = _scale(client, admin, "P21275_EDIT", GOOD_ITEMS)
    assert created.status_code == 201, created.text
    resp = client.patch(f"{B}/scales/{created.json()['id']}", headers=admin, json={"items": DUP_ITEMS})
    assert (resp.status_code, resp.json()) == (422, {"detail": DETAIL}), resp.text   # 修前 200


@pytest.fixture(scope="module")
def legacy(client, admin):
    """修前落库的同名选项量表：一份草稿、一份已发布，外加作答用的患者与机构。"""
    from app.database import SessionLocal
    from app.spd.models import SpdScale

    with SessionLocal() as db:
        draft = SpdScale(code="P21275_DRAFT", name="存量草稿", category="screen", program_code="diabetes",
                         items=DUP_ITEMS, scoring=SCORING, status="draft")
        published = SpdScale(code="P21275_PUB", name="存量已发布", category="screen", program_code="diabetes",
                             items=DUP_ITEMS, scoring=SCORING, status="published", qr_token="P21275-PUB")
        db.add_all([draft, published])
        db.commit()
        draft_id = draft.id
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21275 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21275 患者", "id_card": "330106197501011275", "gender": "女", "birth_date": "1975-01-01"})
    assert patient.status_code in (200, 201), patient.text
    return {"draft": draft_id, "org": org, "patient": patient.json()["id"]}


def test_存量草稿_发布422_改好再发布(client, admin, legacy):
    resp = client.post(f"{B}/scales/{legacy['draft']}/publish", headers=admin)
    assert (resp.status_code, resp.json()) == (422, {"detail": DETAIL}), resp.text   # 修前 200
    fixed = client.patch(f"{B}/scales/{legacy['draft']}", headers=admin, json={"items": GOOD_ITEMS})
    assert fixed.status_code == 200, fixed.text
    assert client.post(f"{B}/scales/{legacy['draft']}/publish", headers=admin).status_code == 200


def _screen(client, admin, legacy, scale_code):
    return client.post(f"{B}/screenings", headers=admin, json={
        "patient_id": legacy["patient"], "program_code": "diabetes", "org_id": legacy["org"],
        "scale_code": scale_code, "answers": {"q1": "是", "q2": "否"}})


def test_存量已发布的照常作答_评分行为不改(client, admin, legacy):
    resp = _screen(client, admin, legacy, "P21275_PUB")
    assert resp.status_code == 201, resp.text   # 不进作答时也查的 scale_problem：不因这次校验整张作答不了
    assert (resp.json()["score"], resp.json()["risk_level"]) == (0, "low")   # 同名照旧取后写的分值（出新版本时改）


def test_正常量表照建照发_照常计分(client, admin, legacy):
    created = _scale(client, admin, "P21275_OK", GOOD_ITEMS)
    assert created.status_code == 201, created.text
    assert client.post(f"{B}/scales/{created.json()['id']}/publish", headers=admin).status_code == 200
    resp = _screen(client, admin, legacy, "P21275_OK")
    assert resp.status_code == 201, resp.text
    assert (resp.json()["score"], resp.json()["risk_level"], resp.json()["result"]) == (3, "high", "suspect")
