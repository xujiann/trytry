"""考核指标的目标与公式结果同一量纲（P2-737，第十九批「单位与量纲」扫描 K4-10）。

`SpdIndicator.score_rule` 的模型注释原先举例 `{"type":"ratio","full":100,"target":0.9}`，而公式与种子都是百分数
（`done / total * 100`、目标 80 / 85 / 90）：照注释通过接口建一个「done / total * 100、规则 target 0.9、不填目标值」的
指标，完成率 5% 也满分（5.0 ≥ 0.9）。注释已改为 90 并写明同一量纲；这里钉住种子自己不自相矛盾——按比例计分、公式乘了
100 的，规则里的目标与目标值都得是百分数写法（大于 1）。
"""
import inspect

import app.spd.models as spd_models
from app.spd.seed import SEED_INDICATORS


def test_种子里乘100的按比例指标_目标都按百分数写():
    wrong = [
        ind["code"] for ind in SEED_INDICATORS
        if (ind.get("score_rule") or {}).get("type") == "ratio" and "* 100" in (ind.get("formula") or "")
        and not all(t is None or t > 1 for t in ((ind.get("score_rule") or {}).get("target"), ind.get("target_value")))
    ]
    assert wrong == [], wrong


def test_模型注释的例子与种子同一量纲():
    source = inspect.getsource(spd_models)   # 注释不进运行时，从源码读
    assert '"target":0.9' not in source and '"target":90' in source
