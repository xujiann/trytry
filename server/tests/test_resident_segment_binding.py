"""居民端在线服务的分段按钮处理函数也绑到了慢专病分段上：点「监测」把服务页的状态清空、白拉一遍转诊清单（P2-365）。

在线服务的分段（`data-svc`）与慢专病页签的分段（`data-spd`）共用 `.seg-btn` 样式；服务那段按 `.seg-btn` 一把绑、
一把清高亮——点慢专病任一分段，`activeService` 变成 undefined、服务页没有高亮，还多拉一遍转诊清单（居民端每读一次
都留一条本人调阅，喂给了 P2-360）。修法：两处选择器都只认带 `data-svc` 的。
"""
import re
from pathlib import Path

M_JS = (Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "m.js").read_text(encoding="utf-8")


def test_服务分段只绑带data_svc的按钮():
    block = M_JS[M_JS.index("activeService = btn.dataset.svc") - 200: M_JS.index("activeService = btn.dataset.svc") + 200]
    selectors = re.findall(r'querySelectorAll\("([^"]+)"\)', block)
    assert selectors and all(sel == ".seg-btn[data-svc]" for sel in selectors), selectors   # 修前 ".seg-btn"
