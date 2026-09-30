"""请求体里的孤立代理不再让 422 变成 500（P2-1158，第三十三批「生僻字、emoji 与 Unicode 边界」扫描 A3-6 的渲染那一半）。

JSON 里的 `"\\ud83d"`（JS `slice` / Java `substring` 截在 emoji 中间再 `JSON.stringify` 就是这个样子）经 `json.loads` 照收成一个
落单的代理码点。校验拦下之后，错误详情的 `input` 里就带着它，按 UTF-8 编码 422 响应时 UnicodeEncodeError——校验明明拦住了，
调用方却只看到「Internal Server Error」，与 P1-92（错误详情里的 NaN）同一个形状。修后错误详情里的孤立代理换成 U+FFFD，其余
字节不变。没有约束、校验放行之后才在业务里炸的字段（电话、口令）与生产库的 NUL 另待裁定。
"""
import json

import pytest

from app.main import _finite_json

URL = "/api/patients"


def _raw_post(client, headers, body: str):
    return client.post(URL, content=body.encode("utf-8"), headers={**headers, "Content-Type": "application/json"})


@pytest.mark.parametrize("body", [
    '{"name": "\\ud83d"}',                                   # 缺证件号：input 是整个请求体，里面带着孤立代理
    '{"name": 5, "id_card": "\\udc00"}',                     # 类型不对的字段旁边的字段带孤立代理
    '{"\\ud83d": 1}',                                        # 键里带孤立代理
])
def test_错误详情里带孤立代理_照样422而不是500(client, admin, body):
    resp = _raw_post(client, admin, body)
    assert resp.status_code == 422, resp.text   # 修前 500
    text = resp.content.decode("utf-8")          # 能按 UTF-8 解开，说明响应里没有孤立代理
    assert "�" in text


def test_成对的代理照原样_响应字节不变():
    # 成对的代理经 json.loads 合成一个字符（U+1F600），不是孤立代理，原样留着
    detail = [{"type": "missing", "loc": ["body", "id_card"], "msg": "Field required", "input": json.loads('{"name": "\\ud83d\\ude00"}')}]
    assert _finite_json(detail) == detail
    assert _finite_json({"name": "张三", "n": 1.5}) == {"name": "张三", "n": 1.5}
    assert _finite_json("a\ud83db") == "a�b"
