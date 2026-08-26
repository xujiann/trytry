#!/usr/bin/env node
/*
 * 管理端页面渲染比对器（ADR-0009 第二步的取证工具）。
 *
 * 为什么需要它：把手写的 `<div class="panel"><h3>…</h3>` 外壳换成 `panel()` 调用，
 * **本该是 no-op**。但"本该"不是证据——外壳里常混着条件属性（accent 边框）、
 * 标题里插着服务端数据、`table()` 的调用被折行折进外壳中间。肉眼比对 60 行模板
 * 字符串会漏，所以每迁一页就跑一次这个：在 Node 里把页面真渲染出来，
 * 拿迁移前后的 innerHTML **逐字符比**。
 *
 * 它不是测试，是**取证工具**——前端免构建、没有 jest（ADR-0009 Risk 段），
 * 持久化的守卫仍然走 `tests/test_frontend_panel_component.py` 那套静态扫描。
 * 这里做的是静态扫描做不到的事：证明输出字节没变。
 *
 * **这不违反"免构建"约束**，那条约束说的是**交付给浏览器的前端**不需要打包器
 * ——`index.html` 里仍然是一串裸 `<script src>`，本文件一个字节都不会被送到
 * 浏览器。它是开发侧手工跑的取证脚本，不进 CI、不进 Makefile、不加任何 npm
 * 依赖（只用 Node 内置的 fs/vm/path），删掉它对应用没有任何影响。
 *
 * 用法：
 *   node scripts/render_diff.js <renderFn> [<renderFn> …]      # 工作区 vs HEAD
 *   node scripts/render_diff.js --base <git-ref> <renderFn> …   # 工作区 vs 指定版本
 *   node scripts/render_diff.js --list                          # 列出有夹具的页面
 *
 * 夹具在 fixtures/render_fixtures.json：按接口路径前缀给假数据。**夹具必须造出
 * 非空数据**——空列表下 `table()` 走"暂无数据"分支，什么都比不出来（这个坑上一轮
 * 在契约治理里踩过：空集钉不住字段）。
 *
 * 注意：`--base` 取的是 git 里那一版的 static/，与工作区同一套夹具、同一个
 * 渲染路径，差异只可能来自页面代码本身。
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { execFileSync } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const STATIC_REL = "server/app/static";
const FILES = ["shared.js", "core.js", "pages-clinical.js", "pages-public.js", "pages-mgmt.js", "pages-spd.js"];
const FIXTURES = Object.fromEntries(
  Object.entries(JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "render_fixtures.json"), "utf8")))
    .filter(([key]) => !key.startsWith("_")),   // `_readme` 之类的说明键不是页面
);

/* ---------------- DOM 替身 ----------------
 * 只需要 #page-body 的 innerHTML，其余一律吞掉：render 函数会给表单挂 onsubmit、
 * 给容器挂 onclick、往 #page-desc 写 textContent，这些都与"渲染出的 HTML"无关。
 * 用 Proxy 兜住未知属性，免得为每个页面补一遍替身的空缺。 */
function makeElement(id) {
  const el = {
    id,
    innerHTML: "",
    textContent: "",
    value: "",
    dataset: {},
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {},
    removeEventListener() {},
    appendChild() {},
    setAttribute() {},
    getAttribute: () => null,
    querySelector: () => makeElement("child"),
    querySelectorAll: () => [],
    closest: () => null,
    focus() {},
    click() {},
    remove() {},
    checked: false,
    children: [],
    options: [],
  };
  return new Proxy(el, {
    get(target, prop) {
      if (prop in target) return target[prop];
      if (typeof prop === "symbol") return undefined;
      return function () {};   // 未知方法一律 no-op
    },
    set(target, prop, value) { target[prop] = value; return true; },
  });
}

function buildSandbox(apiFixture) {
  const elements = new Map();
  const el = (sel) => {
    if (!elements.has(sel)) elements.set(sel, makeElement(sel));
    return elements.get(sel);
  };
  const store = new Map();
  const sandbox = {
    console,
    document: {
      querySelector: el,
      querySelectorAll: () => [],
      createElement: () => makeElement("created"),
      getElementById: (id) => el("#" + id),
      body: makeElement("body"),
      cookie: "",
      addEventListener() {},
    },
    window: { addEventListener() {}, location: { hash: "" }, matchMedia: () => ({ matches: false }) },
    location: { hash: "", href: "http://localhost/", reload() {} },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: async () => { throw new Error("渲染比对不该真发请求——api() 已被替身接管"); },
    setInterval: () => 0,
    clearInterval() {},
    setTimeout: (fn) => { return 0; },
    clearTimeout() {},
    alert() {}, confirm: () => true, prompt: () => "x",
    URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
    Blob: function () {},
    __apiFixture: apiFixture,
    __elements: elements,
  };
  sandbox.globalThis = sandbox;
  return vm.createContext(sandbox);
}

/** 夹具查表：先精确匹配，再按最长前缀匹配（路径带 query 时很常见）。 */
function fixtureFor(page, url) {
  const table = FIXTURES[page] && FIXTURES[page].api;
  if (!table) throw new Error(`夹具里没有页面 ${page}`);
  if (Object.prototype.hasOwnProperty.call(table, url)) return table[url];
  let best = null;
  for (const key of Object.keys(table)) {
    if (url.startsWith(key) && (best === null || key.length > best.length)) best = key;
  }
  if (best === null) throw new Error(`页面 ${page} 的夹具缺少接口 ${url}`);
  return table[best];
}

function render(staticDir, page, fnName) {
  const ctx = buildSandbox(null);
  for (const f of FILES) {
    const src = fs.readFileSync(path.join(staticDir, f), "utf8");
    vm.runInContext(src, ctx, { filename: f });
  }
  // 页面文件加载完再覆盖 api()——core.js 里的定义是 `async function api`，
  // 直接赋值即可盖掉（函数声明可写）。
  const seen = [];
  ctx.api = async (url) => { seen.push(url); return JSON.parse(JSON.stringify(fixtureFor(page, url))); };
  for (const [k, v] of Object.entries((FIXTURES[page] && FIXTURES[page].localStorage) || {})) {
    ctx.localStorage.setItem(k, v);
  }
  const fn = ctx[fnName];
  if (typeof fn !== "function") throw new Error(`找不到渲染函数 ${fnName}`);
  return Promise.resolve(fn()).then(() => ({
    html: ctx.__elements.get("#page-body") ? ctx.__elements.get("#page-body").innerHTML : "",
    desc: ctx.__elements.get("#page-desc") ? ctx.__elements.get("#page-desc").textContent : "",
    calls: seen,
  }));
}

/** 把 <base> 版本的 static/ 导出到临时目录（不碰工作区）。 */
function exportBase(ref) {
  const dir = fs.mkdtempSync(path.join(require("os").tmpdir(), "renderdiff-"));
  for (const f of FILES) {
    const buf = execFileSync("git", ["show", `${ref}:${STATIC_REL}/${f}`], { cwd: ROOT, maxBuffer: 64 * 1024 * 1024 });
    fs.writeFileSync(path.join(dir, f), buf);
  }
  return dir;
}

/** 忽略标签之间的纯空白差异——换行与缩进不进 DOM，改模板必然动它们。 */
function normalize(html) {
  return html.replace(/>\s+</g, "><").replace(/\s+/g, " ").trim();
}

function firstDiff(a, b) {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) if (a[i] !== b[i]) return i;
  return a.length === b.length ? -1 : n;
}

async function main() {
  const argv = process.argv.slice(2);
  if (argv[0] === "--list") {
    for (const [page, spec] of Object.entries(FIXTURES)) console.log(`${page}\t${spec.fn}`);
    return;
  }
  let base = "HEAD";
  if (argv[0] === "--base") { base = argv[1]; argv.splice(0, 2); }
  const pages = argv.length ? argv : Object.keys(FIXTURES);

  const baseDir = exportBase(base);
  const workDir = path.join(ROOT, STATIC_REL);
  let bad = 0;
  for (const page of pages) {
    const spec = FIXTURES[page];
    if (!spec) { console.log(`✗ ${page}: 夹具里没有这一页`); bad++; continue; }
    // 两侧分开渲染、分开判。"旧版直接抛异常、新版渲染正常"是**修好了崩溃**，
    // 不是比对失败——早先把两次渲染写在同一个 try 里，修 `renderMedication`
    // 那个白屏 bug 时它报的是"渲染失败"，把修复本身当成了故障。
    const [before, after] = await Promise.all(
      [baseDir, workDir].map((dir) => render(dir, page, spec.fn).catch((e) => ({ error: e.message }))),
    );
    if (before.error && after.error) {
      console.log(`✗ ${page}: 两版都渲染不出来 —— ${after.error}`);
      bad++; continue;
    }
    if (before.error) {
      console.log(`✓ ${page} (${spec.fn}): 旧版抛「${before.error}」，新版渲染正常——崩溃已修`);
      continue;
    }
    if (after.error) {
      console.log(`✗ ${page}: 新版渲染失败（旧版正常）—— ${after.error}`);
      bad++; continue;
    }
    const a = normalize(before.html), b = normalize(after.html);
    if (!a.length) { console.log(`✗ ${page}: 迁移前渲染为空，夹具没造出数据，比对无意义`); bad++; continue; }
    if (a === b) {
      console.log(`✓ ${page} (${spec.fn}): 忽略标签间空白后逐字符相同（${a.length} 字符, ${after.calls.length} 个接口）`);
    } else {
      const i = firstDiff(a, b);
      console.log(`✗ ${page} (${spec.fn}): 输出有差异，第 ${i} 字符起`);
      console.log(`  前: …${a.slice(Math.max(0, i - 60), i + 90)}`);
      console.log(`  后: …${b.slice(Math.max(0, i - 60), i + 90)}`);
      bad++;
    }
    if (before.desc !== after.desc) { console.log(`  ! #page-desc 变了：\n    前: ${before.desc}\n    后: ${after.desc}`); bad++; }
  }
  fs.rmSync(baseDir, { recursive: true, force: true });
  process.exit(bad ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(2); });
