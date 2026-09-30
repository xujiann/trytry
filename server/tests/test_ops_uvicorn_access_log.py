"""生产启动的 uvicorn 访问日志把查询串原样打到 stdout：按身份证号检索患者时，证件号明文进日志（P2-1142，第三十三批扫描
A2-3）。

`start.sh` 与运维手册的裸机 / systemd / 切库后启动命令都是 `uvicorn … --host … --port …`，没关 uvicorn 自带的访问日志：
`uvicorn.access` 把 path 连查询串整行写 stdout（docker / journald 留存），PII 加密开关管不到这里，中文姓名也只是百分号
编码。管理端「姓名/身份证/健康卡号」检索走 `GET /api/patients?keyword=`，慢专病在管档案筛选走 `/api/spd/enrollments?keyword=`，
接口本身还收 `id_card=`、`phone=`。修前实测（scan33 a2/r1，按启动参数进程内起 uvicorn）：stdout 里是
`"GET /api/patients?keyword=330102199001011234 HTTP/1.1" 200 OK`，同一请求的 medplat.access 只有 `"path": "/api/patients"`。

修法：启动命令一律加 `--no-access-log`——平台自己的访问日志 medplat.access 只记 path，方法、状态、耗时、request_id 都在；
运维手册补一句反向代理日志别记查询串（nginx 用 `$uri`）。本文件钉住 start.sh 与运维手册里每一条起服务的 uvicorn 命令。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
START_SH = ROOT / "server" / "start.sh"
RUNBOOK = ROOT / "docs" / "运维手册.md"


def _launches(path: Path) -> list[str]:
    """文件里每一条起服务的 uvicorn 命令行。"""
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if "uvicorn app.main:app" in line]


def test_start_sh每条启动命令都关了uvicorn访问日志():
    lines = _launches(START_SH)
    assert len(lines) == 2, lines   # 单 worker、多 worker 各一条——少了说明扫描对象变了，本用例在空转
    missing = [line for line in lines if "--no-access-log" not in line.split()]
    assert missing == [], f"这些启动命令会让 uvicorn 把查询串（证件号、手机号）写进 stdout：{missing}"


def test_运维手册每条启动命令都关了uvicorn访问日志():
    lines = _launches(RUNBOOK)
    assert len(lines) == 3, lines   # 1.3 裸机、systemd 单元、1.5 切 PG 后启动
    missing = [line for line in lines if "--no-access-log" not in line.split()]
    assert missing == [], f"照手册起的服务会把查询串写进 stdout：{missing}"


def test_运维手册写明反向代理日志别记查询串():
    text = RUNBOOK.read_text(encoding="utf-8")
    line = next((ln for ln in text.splitlines() if "$uri" in ln), "")
    assert "查询串" in line and "$request" in line, "手册没写反向代理访问日志要换掉带查询串的 $request"
