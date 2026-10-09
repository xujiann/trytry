"""患者主索引页（`core.js::renderPatients`）原样拿到 node 里跑的夹具（P2-1725 起共用，第五十一批扫描 AO2）。

页面函数与它用到的 `table`、`panel`、`setMsg`、`registerConflicts`（core.js）、`formJson`（pages-clinical.js）、`spdModal`
（pages-spd.js）都取自源文件原文（连同 `shared.js`）。DOM 垫片与请求管道同住院管理页，取自 `inpatient_page.py`：GET 一律转给
真接口，写请求记下（方法、地址、请求体）、缺省不发，`send_writes=True` 时照发、把真回执（或报错）交回页面。
"""
import json
import subprocess

from inpatient_page import PRELUDE, _detail, _read, function_source


def script(steps: str) -> str:
    core, clinical, spd = _read("core.js"), _read("pages-clinical.js"), _read("pages-spd.js")
    return (
        PRELUDE + _read("shared.js") + "\n"
        + function_source(spd, "function spdModal(")
        + function_source(core, "function table(") + function_source(core, "function panel(")
        + function_source(core, "function setMsg(") + function_source(core, "function registerConflicts(")
        + function_source(clinical, "function formJson(")
        + function_source(core, "async function renderPatients(")
        + f"\n(async () => {{\n{steps}\n}})().then((r) => {{ process.stdout.write(JSON.stringify({{ result: r }}) + '\\n');"
        + " rl.close(); }, (e) => { console.error(e); process.exit(1); });\n"
    )


def run(client, headers, steps: str, params: dict | None = None, *, send_writes: bool = False):
    """在 node 里加载患者主索引页的代码跑 `steps`——async 函数体，return 一个可 JSON 化的值；`params` 在 node 里是
    `ARGS.params`。返回 `(结果, 页面发出的请求 [(方法, 地址)])`；请求以 `headers` 的身份转给真接口（写请求见模块说明）。"""
    payload = json.dumps({"params": params or {}}, ensure_ascii=False)
    proc = subprocess.Popen(["node", "-e", script(steps), payload], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    requests = []
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                proc.wait(timeout=30)
                raise AssertionError(f"node 没给出结果就退出了：{proc.stderr.read()}")
            message = json.loads(line)
            if "result" in message:
                return message["result"], requests
            requests.append((message["method"], message["path"]))
            assert len(requests) <= 50, f"请求停不下来：{requests}"
            if message["method"] == "GET" or send_writes:
                resp = client.request(message["method"], message["path"], json=message["body"], headers=headers)
                total = resp.headers.get("X-Total-Count")
                reply = ({"data": resp.json(), "total": None if total is None else int(total)} if resp.status_code < 400
                         else {"error": _detail(resp), "status": resp.status_code})
            else:
                reply = {"data": {}}
            proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
