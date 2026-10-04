"""体质辨识接口拼错的维度键不再悄悄丢掉；answers 与 scores 二选一（P2-1410，第四十一批扫描 AE4-8）。

修前 `answers` 里认不得的键直接 `continue`、`scores` 只留认得的键，只有**全部**键都认不得时才 422：送
`{"qi_deficency": 70, "yin_deficiency": 35}`（qi_deficiency 拼错一个字母）判平和质，气虚 70 悄无声息地没了——拼对的同一份
请求判气虚质、给补中益气汤。`answers` 与 `scores` 同时送时，有 answers 就不看 scores：简表交阴虚、直报交气虚 70，判平和质、
score 0。页面用 `/constitution/spec` 的键拼 `scores`，碰不到；直接调接口的对接方会碰到。

修后：有一个认不得的维度键就 422，并列出可用的维度键（与 spec 同一份 `CONSTITUTIONS`）；两种都送 422「二选一」。平和质
`balanced` 是认得的键：收不收、怎么判随 P2-1060 待裁定，照旧收下、不进判定。正确请求的判定结果一字不变。
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
#: 简表八条全「总是」：转化分 100
ALL_FIVE = [5] * 8


@pytest.fixture(scope="module")
def allowed(client, admin):
    """可用的维度键（照 spec 的次序与名称）：422 的报错里列的就是这一串。"""
    spec = client.get("/api/tcm/constitution/spec", headers=admin).json()
    return "、".join(f"{c['key']}（{c['name']}）" for c in spec["constitutions"])


def _identify(client, admin, body):
    return client.post("/api/tcm/constitution", headers=admin, json=body)


def test_直报拼错一个键_422并列出可用键(client, admin, allowed):
    resp = _identify(client, admin, {"scores": {"qi_deficency": 70, "yin_deficiency": 35}})
    assert resp.status_code == 422, resp.text   # 修前 200：判平和质，气虚 70 悄悄丢了
    assert resp.json() == {"detail": f"体质维度只认 {allowed}，不认：qi_deficency"}
    assert "balanced（平和质）" in allowed and allowed.count("（") == 9


def test_简表拼错键也422_认不得的逐个列出(client, admin, allowed):
    resp = _identify(client, admin, {"answers": {"yin_deficency": ALL_FIVE, "qi_deficiency": ALL_FIVE,
                                                 "Qi_Stagnation": ALL_FIVE}})
    assert resp.status_code == 422, resp.text   # 修前 200：只按气虚一维判
    assert resp.json() == {"detail": f"体质维度只认 {allowed}，不认：yin_deficency、Qi_Stagnation"}


def test_两种都送_422二选一(client, admin):
    resp = _identify(client, admin, {"answers": {"yin_deficiency": [1] * 8}, "scores": {"qi_deficiency": 70}})
    assert resp.status_code == 422, resp.text   # 修前 200：整份 scores 被丢，判平和质、score 0
    assert resp.json() == {"detail": "scores（转化分）与 answers（简表条目得分）二选一，不能同时送"}


def test_正确请求的判定结果不变(client, admin):
    direct = _identify(client, admin, {"scores": {"qi_deficiency": 70, "yin_deficiency": 35}})
    assert direct.status_code == 200, direct.text
    assert direct.json() == {
        "constitution": "气虚质", "score": 70, "transformed_scores": {"qi_deficiency": 70, "yin_deficiency": 35},
        "tendencies": ["阴虚质"], "also": [], "name": "气虚质",
        "advice": "补中益气，忌过劳；食疗：山药、大枣、黄芪炖鸡", "formula": "补中益气汤",
    }
    form = _identify(client, admin, {"answers": {"qi_stagnation": [4] * 7, "qi_deficiency": [2, 2, 1, 1, 2, 1, 2, 1]}})
    assert form.status_code == 200, form.text
    body = form.json()
    assert (body["constitution"], body["transformed_scores"]) == ("气郁质", {"qi_stagnation": 75, "qi_deficiency": 12})


def test_平和质照旧收下_不进判定(client, admin):
    """平和质收不收、怎么判是 P2-1060 的待裁定：本条只收紧认不得的键，平和质照修前的口径——认得、不进判定。"""
    alone = _identify(client, admin, {"scores": {"balanced": 50}})
    assert alone.status_code == 422 and alone.json() == {"detail": "缺少有效的体质维度得分"}, alone.text
    mixed = _identify(client, admin, {"scores": {"balanced": 80, "qi_deficiency": 45}})
    assert mixed.status_code == 200, mixed.text
    assert (mixed.json()["constitution"], mixed.json()["transformed_scores"]) == ("气虚质", {"qi_deficiency": 45})


def test_页面只用spec的偏颇体质键拼scores_不送answers():
    """页面不受影响：维度框按 spec 的键画、按同一组键取值，请求体只有 scores。"""
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = source.index("async function renderTcm()")
    body = source[start:source.index("\nasync function ", start + 1)]
    assert 'const BIASED = spec.constitutions.filter((c) => c.key !== "balanced");' in body
    assert '<input name="${esc(c.key)}" type="number"' in body
    assert "BIASED.forEach((c) => { const v = f.get(c.key); if (v !== \"\" && v !== null) scores[c.key] = Number(v); });" in body
    assert 'api("/api/tcm/constitution", { method: "POST", body: JSON.stringify({ scores }) })' in body
    assert "answers" not in body[body.index('$("#tcm-const").onsubmit'):body.index('$("#tcm-order").onsubmit')]
