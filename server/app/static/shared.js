/* 三套前端共用的最小工具层（ADR-0009 第一步）。

   本仓库有三个免构建的原生 JS 前端——管理端 `core.js`+`pages-*.js`、
   居民端 `m/m.js`、医生端 `m/doctor.js`。此前它们各自抄了一份 `$` 与 `esc`，
   三份实现逐字相同。

   合并的动机**不是整洁**，是安全：`esc()` 是 CLAUDE.md §8 的红线
   （"前端渲染用户数据一律先 esc()"），而全仓库有近百处手写 innerHTML 插值。
   同一个函数存三份，就有三个地方可能被改坏、被漏改；本轮之前已经出过一次
   "改一处漏两处"。一份实现意味着一处审查点。

   **加载顺序**：本文件必须最先加载（三个 HTML 入口都已排在第一个 script）。
   后面的文件里不得再声明 `$` / `esc`——同作用域重复 `const` 声明是
   SyntaxError，整个页面会白屏。`tests/test_frontend_shared_utils.py` 盯着这条。

   刻意**没有**并进来的：`api()`。三套的 `api` 看着像，实际认证语义各不相同——
   管理端令牌在 localStorage、医生端在 sessionStorage、居民端 `api()` 根本不带令牌
   （另有 `authApi`）；连 401 的处理时机与文案都不一样（管理端先判 401 再解析
   响应体，医生端反过来）。把它们捏成一个函数需要把令牌来源与 401 回调都参数化，
   那是**行为重构**而不是去重，混进这一步会让"只是合并重复代码"这句话变成假话。
   留作 ADR-0009 的后续步骤单独做。 */
"use strict";

/** 选择器简写。三套前端此前各自声明过一份，实现逐字相同。 */
const $ = (sel) => document.querySelector(sel);

/**
 * HTML 转义。**所有插进 innerHTML 的用户数据都必须先过这里**（CLAUDE.md §8）。
 *
 * `?? ""` 让 null/undefined 变成空串而不是字面量 "null"；单引号也要转义，
 * 因为属性值用单引号包裹的写法在本仓库里是存在的。
 */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/**
 * 状态标签：`<span class="tag 配色">文案</span>`（ADR-0009 第二步，P2-26）。
 *
 * **这是"把转义收进组件就漏不掉"的第二个样本**，而且是有血的那个：此前全仓库
 * 有 33 处在手写它——
 *
 *     const [text, color] = MAP[x.status] || [x.status, ""];
 *     `<span class="tag ${color}">${text}</span>`     // ← 少一个 esc()
 *
 * 映射查不到时 `text` 就是后端原始状态码，33 处**一处都没转义**（2026-08-26 修）。
 * 而同一个仓库里 `spdTag()` 早就写对了——只是没铺满。收成一份实现之后，
 * 调用点连"要不要转义"这个问题都不会遇到。
 *
 * 为什么放 shared.js 而不是 core.js（`panel()` 放的是 core.js）：`.panel`/`.card`
 * 是管理端独有的标记约定，而 `.tag` **三套前端都在用**，且标记契约逐字相同
 * （`style.css:60` / `m/m.css:141` 各自定义 `.tag` 与 `.tag.red/.green/.orange`，
 * 配色不同、类名约定一致）。判据始终是"三端是不是真的都在用"，不是"看起来像工具"。
 *
 * 查不到映射时**原样显示状态码**（而不是吞掉），因为那正是"后端加了个新状态、
 * 前端还没跟上"的现场——显示出来才有人去补，吞掉就永远没人知道。
 *
 * 注意兜底用 `key ?? ""` 而**不是** `key || ""`：本仓库有数字状态码
 * （慢病分级是 1/2/3），`0 || ""` 会把 0 吞成空白。这不是假想——
 * `scripts/statustag_equiv.js` 的等价性矩阵当场抓到过这一条。
 *
 * 慢专病历来把空状态显示成 `—` 而不是空白，那个约定保留在它自己的调用点上
 * （`spdTag`），**没有**顺手统一——那是改字节，不是去重。
 *
 * @param map 状态码 → `[文案, 配色类名]`，**前端定义的常量**
 * @param key 后端给的状态码
 */
function statusTag(map, key) {
  const hit = map[key];
  return `<span class="tag ${esc(hit ? hit[1] : "")}">${esc(hit ? hit[0] : key ?? "")}</span>`;
}

/**
 * 读取一个**非 HttpOnly** Cookie 的值（G3 令牌 Cookie 化）。
 *
 * 三套前端都要用它取双提交 CSRF token（medplat_csrf / medplat_portal_csrf），
 * 所以放本文件（与 $ / esc 同一"只许一份实现"的理由）。令牌本体在 HttpOnly
 * Cookie 里，这个函数**读不到**——那正是 P1-23 收口的目的。
 */
function readCookie(name) {
  for (const part of document.cookie.split("; ")) {
    if (part.startsWith(name + "=")) return decodeURIComponent(part.slice(name.length + 1));
  }
  return "";
}

/* 原生表单提交兜底（CI 实锤的一类竞态，2026-08-31）。
 *
 * 三套前端的表单**全部**由 JS 的 submit 监听接管（全仓库没有一个 <form action=...>），
 * 原生提交从来不是本仓库的意图。但"innerHTML 画出表单 → await 取数 → 挂监听"
 * 的写法在页面渲染器里很常见，那两趟网络往返是一扇窗：窗口里点提交按钮或按回车，
 * submit 没人接管，浏览器就走原生 GET——表单数据泄进 URL、整页重载、操作丢失。
 * CI 抓到的现场：管理端"启动路径"表单在慢 runner 上打出
 * `/?enrollment_id=1&template_id=1#spdpath`，POST 根本没发出（两次失败同一 URL 形状）。
 *
 * 这里在 document 层兜住：没被页面监听接管的 submit 一律 preventDefault——
 * 把"导航 + 数据丢失"降级成"这一下没生效"（用户填的内容还在，监听挂上后重按
 * 即可）。页面自己的监听先于 document 收到事件且各自 preventDefault，本兜底
 * 对它们是幂等的。**这不是免死金牌**：渲染器仍应把挂监听放在任何 await 之前
 * （见 pages-spd.js renderSpdPath 的注释与教训），兜底只保证最坏情况不再是丢数据。 */
document.addEventListener("submit", (e) => e.preventDefault());
