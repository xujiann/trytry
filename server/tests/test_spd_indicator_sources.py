"""考核指标的取数口径表只有一份：`service.INDICATOR_SOURCES`（P2-93 接建指标入口时收拢）。

口径与变量原先散在三处：建 / 改指标的 `data_source` 取值范围（`IndicatorIn` 的 pattern）、公式校验用的变量表
（`_metric_names`）、真正取数的 `collect_metrics_batch`。管理端要建指标，口径下拉与「这个口径能引用哪些变量」
的提示再抄一份进前端，就是第四处。现在变量表收进 `INDICATOR_SOURCES`，公式校验与 `GET /api/spd/meta` 都取它；
这里把它与另外两处逐项钉住：

- 取值范围：接口收的口径 == 口径表的键——多一个，建得出指标、计分时退化成「total 恒 0」；少一个，页面下拉里
  选得到、接口 422；
- 变量名：每个口径真正取出来的键 == 口径表列的变量——取数多产出一个，公式引用不到；口径表多列一个，公式校验
  过得去、计分时「未知变量」。
"""
import re

import pytest

from app.spd.service import INDICATOR_SOURCES


def test_接口收的口径就是口径表的键():
    from app.spd.routers.assess import IndicatorIn, IndicatorPatch

    for model in (IndicatorIn, IndicatorPatch):
        pattern = model.model_fields["data_source"].metadata
        text = next(m.pattern for m in pattern if getattr(m, "pattern", None))
        assert set(re.fullmatch(r"\^\((.*)\)\$", text).group(1).split("|")) == set(INDICATOR_SOURCES), model


@pytest.mark.parametrize("source", sorted(INDICATOR_SOURCES))
@pytest.mark.parametrize("object_type", ["org", "village_doctor"])
def test_每个口径取出来的变量就是口径表列的(client, admin, source, object_type):
    from app.database import SessionLocal
    from app.spd.models import SpdIndicator
    from app.spd.routers.assess import collect_metrics_batch

    indicator = SpdIndicator(code=f"SRC_{source}", name=source, data_source=source, object_type=object_type)
    with SessionLocal() as db:
        metrics = collect_metrics_batch(db, indicator, object_type, [1, 2], "2026-09")
    assert {tuple(sorted(m)) for m in metrics.values()} == {tuple(sorted(INDICATOR_SOURCES[source][1]))}


def test_元数据接口给出口径与变量(client, admin):
    body = client.get("/api/spd/meta", headers=admin).json()
    got = {x["key"]: (x["name"], [m["key"] for m in x["metrics"]]) for x in body["indicator_sources"]}
    assert got == {k: (name, list(metrics)) for k, (name, metrics) in INDICATOR_SOURCES.items()}
