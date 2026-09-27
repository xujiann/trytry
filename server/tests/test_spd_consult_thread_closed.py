"""已结束的慢专病咨询打开会话不给回复框（P2-596，第十二批「按钮 vs 状态机」扫描 Z2-3）。

回复接口对已结束的会话 409「该会话已结束」（`care.reply_consult`），个案管理师端打开会话原先照样摆着回复框——写完
一段点「回复」才报错。清单行把会话是否已结束带给会话面板，已结束的只读、写明居民再发消息会开启新会话。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def test_打开会话带上是否已结束():
    assert 'data-consult="${c.id}" data-closed="${c.status === "open" ? 0 : 1}"' in PAGE
    assert 'spdShowConsultThread(Number(open.dataset.consult), open.dataset.closed === "1")' in PAGE


def test_已结束的会话不画回复框():
    start = PAGE.index("async function spdShowConsultThread(consultId, closed = false) {")
    body = PAGE[start:PAGE.index("\n}\n", start)]
    assert body.index("${closed") < body.index('id="spd-consult-reply"')
    assert "会话已结束，不能再回复" in body
    assert "if (closed) return;" in body   # 不然给不存在的表单挂事件，整段抛错
