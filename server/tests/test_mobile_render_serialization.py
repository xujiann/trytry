"""移动端分段渲染串行化的静态守卫（m.js loadSpd / doctor.js loadSpdList）。

## 为什么是静态守卫而不是（只有）e2e

这是个**无报错的竞态**：自查提交的回调里"申请 POST 成功 → await loadSpd()
重画"与用户切分段触发的第二个 loadSpd() 竞写同一个容器，后完成者把新分段
整个盖掉。e2e 曾以约四成概率捕获它，但窗口取决于机器时序——修复后在本容器
里把串行化拆掉，e2e 的同一路径连跑六遍也关不上窗口（申请链路快到断言间隙内
就收尾）。**靠概率的网防不住确定性的拆卸**：谁手快删掉互斥，e2e 大概率照绿。

所以确定性由这里承担：钉住三处（管理端 route() 是被抄的范式，一并钉）都
保有"序号 + 互斥 + 收尾补画"的三件套，且旧的直接分发形状不得回潮。
e2e（test_spd_resident_selfscreen_apply_measure）负责真实路径可用，
本文件负责机制不被拆——两张网各管各的。

变异验证（写入时做过）：把 `if (spdRendering) return` 改成 `if (false)`、
把收尾的 `seq === spdSeq` 比较删掉，本文件对应断言各自转红。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

#: (文件, 入口函数名, 序号变量, 互斥变量, 旧直发形状的特征串)
SITES = [
    ("core.js", "route", "routeSeq", "routing", None),
    ("m/m.js", "loadSpd", "spdSeq", "spdRendering",
     'if (activeSpd === "home") return await renderSpdHome'),
    ("m/doctor.js", "loadSpdList", "dspdSeq", "dspdRendering",
     'if (activeDoctorSpd === "todo") return await loadSpdTodo'),
]


def _fn_body(src: str, name: str) -> str:
    """按花括号配平抠出 async function <name>(...) { ... } 的函数体。"""
    m = re.search(rf"async function {re.escape(name)}\(", src)
    assert m, f"未找到 async function {name}("
    i = src.index("{", m.end())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise AssertionError(f"{name} 花括号不配平")


def test_三处分段渲染都保有序号加互斥加收尾补画():
    for fname, fn, seq, mutex, _ in SITES:
        src = (STATIC / fname).read_text(encoding="utf-8")
        body = _fn_body(src, fn)
        assert f"{seq} += 1" in body, f"{fname}:{fn} 丢了渲染序号自增"
        assert re.search(rf"if \({mutex}\) return", body), (
            f"{fname}:{fn} 丢了互斥早退——并发渲染会竞写同一容器，后完成者盖掉新分段"
        )
        assert f"{mutex} = true" in body and f"{mutex} = false" in body, (
            f"{fname}:{fn} 互斥标志没有成对置位/复位（finally 复位丢了会永久卡死渲染）"
        )
        assert "for (;;)" in body, f"{fname}:{fn} 丢了补画循环"
        assert re.search(rf"if \(seq === {seq}\) break", body), (
            f"{fname}:{fn} 丢了收尾序号比较——渲染期间的新请求会被吞掉，"
            f"最后一次点击的分段画不出来"
        )


def test_旧的直接分发形状不得回潮():
    """`return await renderX(box)` 直发 = 每次调用各画各的、无串行化——正是竞态本体。"""
    for fname, fn, _, _, legacy in SITES:
        if legacy is None:
            continue
        src = (STATIC / fname).read_text(encoding="utf-8")
        assert legacy not in src, (
            f"{fname} 出现了旧的直接分发写法（{legacy!r}）——"
            f"串行化被绕开，竞态回归"
        )
