"""动态探针：一家与任何患者都没有关系的新机构，调全部清单接口，看拿回了谁的患者。

为什么要动态量（P1-73）
-----------------------
静态闸门数的是"函数体里有没有守卫"。P0-24 / P0-32 / P0-33 三处的守卫**都在函数体里**，
只是被包在 `if patient_id is not None:` 里——不带患者号（页面默认就不带）时按全域吐数据，
静态判据一概看不出。P0-23 的清单还拿 `user.id` 做过「只看我的」筛选，一样骗过"绑没绑身份"。
所以换一个问法：**造好数据，换一个毫无关系的账号去调，看它拿回了什么**。

做法
----
1. 起一个独立 uvicorn（临时库），跑 `scripts/seed_demo.py` 灌演示数据；
2. 建一家新机构，建医生 / 经办 / 公卫 / 药师四个账号——它们与任何患者都没有业务关系；
3. 取 OpenAPI 里全部**无路径参数**的 GET（居民端 `/api/portal` 除外），四个账号各调一遍；
4. 响应里出现演示患者的 id（键名以 `patient_id` 结尾）、姓名或健康卡号，就算"看见了"；
5. 与已登记的名单交叉：无身份读接口两层（待裁定）、按设计全县可见的主索引检索。

用法：`python scripts/probe_list_exposure.py`。有**未登记**的暴露时退出码 1。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

#: 按设计全县可见的清单，逐条写理由（与待裁定名单分开：这些不是欠账）。
BY_DESIGN = {
    "patients.py:search_patients":
        "患者主索引检索：Patient 是全县主索引、一人一份档案，检索是新业务（建档、挂号、开单）的入口；"
        "出口经 desensitize 脱敏（非 admin 掩码证件号与电话）。",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _route_keys() -> dict[str, str]:
    """GET 路径 → `文件:函数`（与各棘轮名单同一种写法）。"""
    from fastapi.routing import APIRoute

    from app.main import app

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                yield r
            orig = getattr(r, "original_router", None)
            if orig is not None:
                yield from walk(orig.routes)

    out = {}
    for r in walk(app.routes):
        if "GET" not in r.methods:
            continue
        mod = r.endpoint.__module__
        if mod.startswith("app.spd.routers."):
            name = "spd/" + mod[len("app.spd.routers."):].replace(".", "/") + ".py"
        else:
            name = mod[len("app.routers."):].replace(".", "/") + ".py"
        out[r.path] = f"{name}:{r.endpoint.__name__}"
    return out


def _scan(obj, pids, names, ehcs, found):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("patient_id") and v in pids:
                found.add(k)
            elif isinstance(v, str) and (v in names or v in ehcs):
                found.add(k)
            else:
                _scan(v, pids, names, ehcs, found)
    elif isinstance(obj, list):
        for x in obj:
            _scan(x, pids, names, ehcs, found)
    return found


def probe() -> dict[str, dict[str, list[str]]]:
    with tempfile.TemporaryDirectory() as tmp:
        port = _free_port()
        env = {**os.environ, "MEDPLAT_DATABASE_URL": f"sqlite:///{tmp}/probe.db",
               "MEDPLAT_UPLOAD_DIR": f"{tmp}/uploads", "MEDPLAT_LOG_JSON": "0"}
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                                 "--port", str(port)], cwd=SERVER, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(300):
                try:
                    if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.3)
            seeded = subprocess.run([sys.executable, "scripts/seed_demo.py", base], cwd=SERVER,
                                    capture_output=True, text=True)
            if seeded.returncode != 0:
                raise SystemExit(f"演示种子灌入失败：{seeded.stderr[-500:]}")
            c = httpx.Client(base_url=base, timeout=30)
            admin = c.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).json()
            ah = {"Authorization": f"Bearer {admin['access_token']}"}
            org = c.post("/api/organizations", json={"name": "探针孤岛卫生院", "org_type": "township",
                                                      "level": "township"}, headers=ah).json()
            heads = {}
            for role in ("doctor", "operator", "public_health", "pharmacist"):
                uname = f"probe_{role}"
                c.post("/api/users", json={"username": uname, "password": "pw123456", "full_name": uname,
                                            "role": role, "org_id": org["id"]}, headers=ah)
                tok = c.post("/api/auth/login", json={"username": uname, "password": "pw123456"}).json()
                heads[role] = {"Authorization": f"Bearer {tok['access_token']}"}
            patients = c.get("/api/patients?limit=500", headers=ah).json()
            pids = {p["id"] for p in patients}
            names = {p["name"] for p in patients if len(p["name"]) >= 2}
            ehcs = {p["ehc_no"] for p in patients if p.get("ehc_no")}
            spec = c.get("/openapi.json").json()
            hits: dict[str, dict[str, list[str]]] = {}
            for path, ops in sorted(spec["paths"].items()):
                if "get" not in ops or "{" in path or not path.startswith("/api/") or path.startswith("/api/portal"):
                    continue
                for role, h in heads.items():
                    r = c.get(path, headers=h)
                    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                        continue
                    found = _scan(r.json(), pids, names, ehcs, set())
                    if found:
                        hits.setdefault(path, {})[role] = sorted(found)
            return hits
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def main() -> int:
    import test_unscopable_patient_reads as reads

    registered = (set(reads.UNSCOPABLE_PATIENT_READS) | set(reads.AGGREGATE_ONLY_READS)
                  | set(reads.ONEHOP_UNSCOPABLE_READS))
    keys = _route_keys()
    hits = probe()
    unregistered = []
    for path, roles in sorted(hits.items()):
        key = keys.get(path, f"?{path}")
        tag = "按设计" if key in BY_DESIGN else "待裁定（已登记）" if key in registered else "未登记"
        fields = sorted({f for fs in roles.values() for f in fs})
        print(f"{tag:10s} {key:48s} {path}  角色={','.join(sorted(roles))}  字段={','.join(fields)}")
        if tag == "未登记":
            unregistered.append(key)
    print(json.dumps({"exposed": len(hits), "unregistered": unregistered}, ensure_ascii=False))
    return 1 if unregistered else 0


if __name__ == "__main__":
    raise SystemExit(main())
