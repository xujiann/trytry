"""知情同意书打印件的场景 / 方式文案与业务端同一套码（第十四批「导出 / 打印 vs 页面」扫描 R1-6 核实后的防漂移钉子）。

打印件（`printing.CONSENT_SCENE_NAMES` / `CONSENT_METHOD_NAMES`）用正式全称（「居民健康建档」「居民端本人自签」），
页面与居民端用简称（`consents` 的两张表，P2-73）——措辞不同是有意的：打印件是签字留档的正式文书，
`test_printing_documents.py` 钉着全称。但码只能是同一套：业务端加了新场景、打印件没跟上，打出来就是
`chronic_enroll` 这样的编码。这里钉住两边的键集合一致、且都等于可签场景的白名单。
"""


def test_打印件与业务端的场景码是同一套():
    from app.routers import consents, printing

    assert set(printing.CONSENT_SCENE_NAMES) == set(consents.CONSENT_SCENE_NAMES) == set(consents.CONSENT_SCENES)


def test_打印件与业务端的签署方式码是同一套():
    from app.routers import consents, printing

    assert set(printing.CONSENT_METHOD_NAMES) == set(consents.CONSENT_METHOD_NAMES)
    assert all(v.strip() for v in printing.CONSENT_SCENE_NAMES.values())
