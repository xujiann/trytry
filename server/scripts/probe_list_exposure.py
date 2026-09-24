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

机构维度（2026-09-24 补）
------------------------
同一批调用顺带再问一句：**拿回了哪些别家机构的行**——响应里键名以 `org_id` 结尾、值是演示数据里
已有机构（孤岛卫生院是后建的，不在其中）的，就算看见了别家。首跑 23 条，其中滞留预警是真漏（P0-39 续，
已修），其余与已登记的名单交叉：患者读侧名单、P1-49 待裁定的清单、P0-37 读侧棘轮的两张名单，
再加本文件的 `ORG_BY_DESIGN` / `ORG_AWAITING`。**看不见的**：响应里不带 `*org_id` 键的（例如只带员工号的
统计），这一维按键名认机构，不认名称。

按 id 维度（2026-09-24 补）
--------------------------
清单之外，再按 id 调一遍：全部**路径参数都是整数**的 GET，把参数依次填 1、2、3（演示种子里这些 id 多半
都有行），四个账号各调一遍，按上面两维同一套判法认「看见了」。静态的按 id 读棘轮
（`test_byid_org_read_guard.py` 两层、`test_stage15_horizontal.py` 的患者按 id 读）只认函数体里有没有判定，
**判定包在条件里就看不出**——这一维拿实际响应对一遍。登记名单就是那几道静态棘轮的名单，不另立一份；
首跑 8 条，全在静态名单里。

用法：`python scripts/probe_list_exposure.py`。任一维度有**未登记**的暴露时退出码 1。
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


#: 机构维度：别家机构的行出现在孤岛卫生院的清单里，按设计就是这样的（目录、拓扑、县域监测）。
ORG_BY_DESIGN = {
    "disease_programs.py:list_programs":
        "专病目录（单病种路径定义）：管理员建、全县共用，`org_id` 是主办机构（专病中心所在）而不是归属。",
    "infectious.py:list_cases":
        "传染病病例登记：不含患者个体标识（只记报告机构 / 病种 / 发病日期），县域疫情监测的底数，"
        "与多点触发预警同一口径（一线要看得到辖区疫情）。",
    "infectious.py:late_reports":
        "迟报清单：同一份病例登记的时效视图（无个体），迟报通报本就面向辖区各报告单位。",
    "org_groups.py:list_groups":
        "机构分组是组织拓扑：`groups_of_org` 的 docstring 写明第九轮「明确不设限」，`lead_org_id` 是牵头单位。",
    "spd/workbench.py:catalog":
        "各端下拉框共用的目录（病种、团队、量表、服务包、专病中心、已发布路径）：与逐个目录接口同一口径。",
}

#: 机构维度：该按什么范围给看要人定（写明出处），答之前不改。
ORG_AWAITING = {
    "cssd.py:list_batches":
        "消毒供应批次清单：「谁算中心」没有建模，批次对申领方可不可见随共享中心那一问一起答（待裁定清单 P1-71 问题 2、P1-75）。",
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


def _scan_orgs(obj, other_orgs, found):
    """键名以 `org_id` 结尾、值落在别家机构里的键。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("org_id") and isinstance(v, int) and v in other_orgs:
                found.add(k)
            else:
                _scan_orgs(v, other_orgs, found)
    elif isinstance(obj, list):
        for x in obj:
            _scan_orgs(x, other_orgs, found)
    return found


def probe() -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, list[str]]],
                     dict[str, dict[str, list[str]]]]:
    """(患者维度命中, 机构维度命中, 按 id 命中)：路径 → 角色 → 命中的键。"""
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
            other_orgs = {o["id"] for o in c.get("/api/organizations?limit=500", headers=ah).json()}
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
            org_hits: dict[str, dict[str, list[str]]] = {}
            for path, ops in sorted(spec["paths"].items()):
                if "get" not in ops or "{" in path or not path.startswith("/api/") or path.startswith("/api/portal"):
                    continue
                for role, h in heads.items():
                    r = c.get(path, headers=h)
                    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                        continue
                    body = r.json()
                    found = _scan(body, pids, names, ehcs, set())
                    if found:
                        hits.setdefault(path, {})[role] = sorted(found)
                    found_orgs = _scan_orgs(body, other_orgs, set())
                    if found_orgs:
                        org_hits.setdefault(path, {})[role] = sorted(found_orgs)
            byid_hits: dict[str, dict[str, list[str]]] = {}
            for path, ops in sorted(spec["paths"].items()):
                if "get" not in ops or "{" not in path or not path.startswith("/api/") or path.startswith("/api/portal"):
                    continue
                params = [p for p in ops["get"].get("parameters", []) if p.get("in") == "path"]
                if not params or any((p.get("schema") or {}).get("type") != "integer" for p in params):
                    continue
                for i in (1, 2, 3):
                    url = path
                    for param in params:
                        url = url.replace("{" + param["name"] + "}", str(i))
                    for role, h in heads.items():
                        r = c.get(url, headers=h)
                        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                            continue
                        body = r.json()
                        found = _scan(body, pids, names, ehcs, set()) | _scan_orgs(body, other_orgs, set())
                        if found:
                            seen = byid_hits.setdefault(path, {}).setdefault(role, [])
                            byid_hits[path][role] = sorted(set(seen) | found)
            return hits, org_hits, byid_hits
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def _report(title: str, hits, keys, by_design, registered) -> list[str]:
    print(f"== {title}")
    unregistered = []
    for path, roles in sorted(hits.items()):
        key = keys.get(path, f"?{path}")
        tag = "按设计" if key in by_design else "待裁定（已登记）" if key in registered else "未登记"
        fields = sorted({f for fs in roles.values() for f in fs})
        print(f"{tag:10s} {key:48s} {path}  角色={','.join(sorted(roles))}  字段={','.join(fields)}")
        if tag == "未登记":
            unregistered.append(key)
    return unregistered


def main() -> int:
    import test_list_pagination_ratchet as pagination
    import test_org_param_read_guard as orgread
    import test_unscopable_patient_reads as reads

    registered = (set(reads.UNSCOPABLE_PATIENT_READS) | set(reads.AGGREGATE_ONLY_READS)
                  | set(reads.ONEHOP_UNSCOPABLE_READS))
    org_by_design = set(ORG_BY_DESIGN) | set(orgread.BY_DESIGN)
    # 机构维度的「已登记」：患者读侧名单（真正要守的是患者）、P1-49 待裁定的清单、P0-37 待裁定、本文件待裁定
    org_registered = (registered | set(pagination.HELD_PENDING_SCOPE_DECISION)
                      | set(orgread.AWAITING) | set(ORG_AWAITING))
    import test_byid_org_read_guard as byidread
    import test_stage15_horizontal as horizontal

    # 按 id 维度的登记名单 = 几道静态按 id 读棘轮的名单（按设计的与待裁定 / 别处在盯的分开认）
    byid_by_design = (set(byidread.BY_DESIGN) | set(byidread.BLINDSPOT_BY_DESIGN)
                      | set(horizontal.BYID_PATIENT_READ_OK) | set(horizontal.UNGUARDED))
    byid_registered = (set(byidread.ELSEWHERE) | set(byidread.AWAITING) | set(byidread.BLINDSPOT_ELSEWHERE)
                       | set(byidread.BLINDSPOT_AWAITING) | set(horizontal.NEWLY_VISIBLE_UNGUARDED_READS)
                       | registered)
    keys = _route_keys()
    hits, org_hits, byid_hits = probe()
    unregistered = _report("患者维度：拿回了谁的患者", hits, keys, BY_DESIGN, registered)
    org_unregistered = _report("机构维度：拿回了哪些别家机构的行", org_hits, keys, org_by_design, org_registered)
    byid_unregistered = _report("按 id：填 1/2/3 拿回了别家的患者或机构行", byid_hits, keys, byid_by_design,
                                byid_registered)
    print(json.dumps({"exposed": len(hits), "unregistered": unregistered,
                      "org_exposed": len(org_hits), "org_unregistered": org_unregistered,
                      "byid_exposed": len(byid_hits), "byid_unregistered": byid_unregistered},
                     ensure_ascii=False))
    return 1 if unregistered or org_unregistered or byid_unregistered else 0


if __name__ == "__main__":
    raise SystemExit(main())
