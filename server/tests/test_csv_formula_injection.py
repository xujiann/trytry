"""服务端 CSV 导出防公式注入（P2-63）。

平台的三份服务端导出（运营月报、传染病报告卡、死因报告卡）都经 `reports._csv_response`，原样把单元格写进 CSV。
导出里的文本不全是系统生成的：死因诊断（`MedicalCert.detail`）、姓名、机构名都是人填的。一格以 `=` 开头的
「=HYPERLINK("http://…","点此核对")」，管理层用 Excel / WPS 打开导出文件就成了一个可点的外链，`=cmd|…` 一类还能触发
DDE。慢专病的前端导出（`pages-spd.js::spdDownloadCsv`）早就防了这一手——以 = + - @ 开头的非数字文本前置单引号——
服务端这条没有，同一个平台两套口径。

修法：`_csv_cell` 与前端同一条规则；数字（含负数「-12.50」这类结余）原样导出，只处理文本。
"""
import csv
import io

import pytest

from app.database import SessionLocal
from app.models import MedicalCert, Organization, User
from app.routers.reports import _csv_cell


@pytest.mark.parametrize("raw, expected", [
    ('=HYPERLINK("http://x","点此核对")', '\'=HYPERLINK("http://x","点此核对")'),
    ("+86 138 0000 0000", "'+86 138 0000 0000"),
    ("@SUM(A1:A2)", "'@SUM(A1:A2)"),
    ("\t=HYPERLINK(\"http://x\")", "'\t=HYPERLINK(\"http://x\")"),   # 制表符开头（P2-116）：有的表格软件先吃掉空白再求值
    ("\r=1+1", "'\r=1+1"),
    ("-", "'-"),
    ("-12.50", "-12.50"),          # 负数照原样：结余列不许变成文本
    ("+3", "+3"),
    ("1e5", "1e5"),
    ("急性心肌梗死", "急性心肌梗死"),
    ("", ""),
    (12.5, 12.5),
    (-3, -3),
])
def test_单元格规则与前端同口径(raw, expected):
    assert _csv_cell(raw) == expected


def test_死因报告卡导出_公式开头的死因诊断不再原样写进单元格(client, admin):
    with SessionLocal() as db:
        org = Organization(name="公式注入卫生院", org_type="township", level="township")
        db.add(org)
        db.flush()
        creator = db.query(User).filter(User.username == "admin").one()
        db.add(MedicalCert(cert_type="death", cert_no="P263-1", name="公式注入死者", event_date="2031-05-05",
                           detail='=HYPERLINK("http://x","点此核对")', org_id=org.id, created_by=creator.id))
        db.commit()
    r = client.get("/api/certs/death-report-cards/export.csv", headers=admin,
                   params={"date_from": "2031-05-01", "date_to": "2031-05-31"})
    assert r.status_code == 200, r.text
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    body = [row for row in rows[1:] if row[0] == "P263-1"]
    assert body and body[0][6] == '\'=HYPERLINK("http://x","点此核对")', body   # 修前原样以 = 开头


def test_前端导出与服务端同一张公式前缀表():
    """两边原先各写一份（服务端 `_FORMULA_LEAD`、前端 `spdDownloadCsv` 的正则字符类），「同一条规则」只靠注释说；
    P2-116 给两边补制表符与回车时对着比了一遍，这里钉住：一边加了、另一边没加即红。"""
    import pathlib
    import re

    from app.routers.reports import _FORMULA_LEAD

    js = (pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")
    m = re.search(r"if \(/\^\[([^\]]+)\]/\.test\(s\) && Number\.isNaN", js)
    assert m, "spdDownloadCsv 的公式前缀判定换了写法，这条比对要跟着改"
    decoded = m.group(1).replace("\\-", "-").encode().decode("unicode_escape")   # 字符类里的 \- 是字面的减号
    assert set(decoded) == set(_FORMULA_LEAD), (decoded, _FORMULA_LEAD)
