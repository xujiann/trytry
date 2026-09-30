"""生命体征入参的跨字段约束（P2-1016）。

逐字段的上下界（不收负数、不越过生理上限，P1-101）各入参模型自己写着；两项之间的关系收在这里，平台各入口共用一句：
慢病随访（`chronic.add_followup`，含对接入站的观测值归档）、住院体温单（`clinical_docs.create_vital`）、急救途中体征
（`emergency.report_vitals`）。
"""


def bp_order_problem(sbp: float | None, dbp: float | None) -> str:
    """收缩压须高于舒张压：两项都测到（都大于 0）时，收缩压不高于舒张压只可能是录错或两框录反。

    慢病随访 135/85 录成 85/135 原先照收，舒张压 135 按「越高越危」定 3 级「高危需转诊评估」、建议上转，档案级别跟着被
    改写（在管名单排序、分级分布、风险评分都变）；120/120 同样定 3 级；住院体温单 70/150 照收。与 P1-101 / P1-214「生理上
    不可能的测量值不收」同一条规矩。0 不参与比较：急救现场心跳骤停、血压测不出记 0 是真实的（`emergency.VitalCreate`），
    别处的 0 由各自的下界挡。返回空串表示没问题。
    """
    if sbp is None or dbp is None or sbp <= 0 or dbp <= 0:
        return ""
    if sbp <= dbp:
        return f"收缩压须高于舒张压（收到 {sbp:g}/{dbp:g}），是否两项录反了？"
    return ""
