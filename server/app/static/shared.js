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
 * 本地日历的「今天」`YYYY-MM-DD`（P2-228）；本月取 `localToday().slice(0, 7)`。
 *
 * 别拿 `new Date().toISOString()` 截日期：那是 **UTC** 的时间——东八区早上 8 点前截到的是昨天，每月 1 日
 * 早上 8 点前截出的月份是上个月。录进库里的日期（复诊的实际日期）与表单、报表的默认日期 / 月份都按本地日历，
 * 与后端 `clock.today()` 同一句。`tests/test_frontend_local_date.py` 盯着。
 */
function localToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

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
 * 接口给的地址是不是 http(s)：画成链接（href）、`window.open` 之前先过这一道（P2-1429）。
 *
 * main.py 的 CSP 为免构建的内联脚本放行了 'unsafe-inline'，`javascript:` 链接点了会在本站执行——只做 `esc()` 挡不住
 * （它转的是引号与尖括号，不管协议）；`data:`、相对路径也不该当外链开。判据是前缀、不分大小写，原样取自收银页付款链接
 * （P2-1021），与后端 `texttypes.HTTP_URL` 同一口径。管理端的收银页付款链接、课件外链（P2-1428）、直播回放（P2-1429）
 * 与居民端「我的宣教」的资料链接（P2-1465）共用，一处判据、一处审查点。
 *
 * 原先放在 core.js（用它的三处都在管理端），注释里写明了「居民端哪天也要判，再挪过来」——居民端的宣教资料链接就是那一天。
 * 医生端眼下不画接口给的外链；它也加载本文件，哪天要画直接用，别再抄一份正则。
 */
function isHttpUrl(url) {
  return /^https?:\/\//i.test(url || "");
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

/**
 * 把后端错误体里的 `detail` 变成一句给人看的话（P2-39）。
 *
 * 三套前端的请求帮手都写着 `throw new Error(data.detail || ...)`。`HTTPException`
 * 的 `detail` 是字符串，这么写没问题；可**请求体/参数校验失败**时 FastAPI 回的 422，
 * `detail` 是**数组**（`[{loc, msg, type}, ...]`），`new Error(数组)` 的 message 是
 * `"[object Object]"`——`datetypes` 里写好的"日期 2026-02-31 不存在（请检查月份天数）"
 * 一个字都到不了用户眼前。全仓库没有 RequestValidationError 处理器，所以这是
 * **所有表单**的校验报错，不只是日期。
 *
 * 修在前端而不是给后端加处理器把 detail 改成字符串：422 的数组形状是 FastAPI 的
 * 标准契约，对接方可能按 `loc` 定位字段——改它是破坏性变更（CLAUDE.md §1.7）。
 *
 * 数组逐条取 `msg`，去掉 pydantic 给自定义校验加的 "Value error, " 前缀，前面标上
 * 字段名（`loc` 去掉 body/query/path 这一层来源），多条用"；"连接。字符串原样返回；
 * 空的或认不出的形状回落到调用方给的兜底文案——宁可笼统，也不要 `[object Object]`。
 *
 * 必填文本只填了空格（P1-109，后端 `texttypes.NON_BLANK`；只有零宽空格、BOM、控制字符这类看不见的字符同一条，P2-1148）
 * pydantic 的原话是 "String should match pattern '[^\s\p{Cc}\p{Cf}]'"，必填文本留空（`min_length=1`，P1-110）的原话是
 * "String should have at least 1 character"——按错误类型与约束认出来换成人话。
 * 写超了长度（`max_length`）原先照样是英文 "String should have at most 512 characters"，批量选多了是
 * "List should have at most 500 items after validation, not 501"（P2-606）——同样按类型换成「最多 N 个字 / 项」。
 * 数值上下界、整数 / 数字解析、缺字段、格式（正则）、取值不在选项里（P2-1087）：原先照样是 pydantic 的英文原话——
 * 孕周填「38+2」报 "Input should be a valid integer, unable to parse string as an integer"、体温敲成 365 报
 * "Input should be less than or equal to 45"，而产检、绩效权重、定时任务等六处页面注释都写着「交给后端报人话」。
 */
function errorText(detail, fallback) {
  const ERROR_TYPE_TEXT = {
    greater_than_equal: (ctx) => `不能小于 ${ctx.ge}`,
    greater_than: (ctx) => `要大于 ${ctx.gt}`,
    less_than_equal: (ctx) => `不能大于 ${ctx.le}`,
    less_than: (ctx) => `要小于 ${ctx.lt}`,
    int_parsing: () => "要填整数",
    int_type: () => "要填整数",
    int_from_float: () => "要填整数，不能带小数",
    float_parsing: () => "要填数字",
    float_type: () => "要填数字",
    bool_parsing: () => "只能选是或否",
    missing: () => "必填",
    literal_error: () => "取值不在可选范围内",
    string_pattern_mismatch: () => "格式不对",
  };
  if (Array.isArray(detail)) {
    const origins = ["body", "query", "path", "header", "cookie"];
    const parts = detail.map((e) => {
      const ctx = (e && e.ctx) || {};
      const blank = e && e.type === "string_pattern_mismatch" && ctx.pattern === "[^\\s\\p{Cc}\\p{Cf}]";
      const empty = e && e.type === "string_too_short" && ctx.min_length === 1;
      const tooLong = e && e.type === "string_too_long" && ctx.max_length != null;
      const tooMany = e && e.type === "too_long" && ctx.max_length != null;
      const byType = e && Object.prototype.hasOwnProperty.call(ERROR_TYPE_TEXT, e.type) ? ERROR_TYPE_TEXT[e.type](ctx) : "";
      const msg = blank ? "不能只填空格" : empty ? "不能为空"
        : tooLong ? `最多 ${ctx.max_length} 个字，超出了` : tooMany ? `最多 ${ctx.max_length} 项，超出了`
        : byType || String((e && e.msg) || "").replace(/^Value error, /, "");
      const field = Array.isArray(e && e.loc)
        ? e.loc.filter((x) => !origins.includes(x)).join(".") : "";
      return field && msg ? `${field}：${msg}` : msg;
    }).filter(Boolean);
    return parts.length ? parts.join("；") : fallback;
  }
  return typeof detail === "string" && detail ? detail : fallback;
}

/**
 * 续页取全一个清单接口（P2-1333）：按 `limit` / `offset` 一页页取，直到一页取不满。
 *
 * 清单接口一页最多 500 条（后端 `deps.paginate` 的上限，总数在 X-Total-Count 响应头里）。在院清单按住院号倒序，
 * 住院页、住院临床文书、医生移动端查房三处原先只取第一页——在院过 500 人，被截掉的正是住得最久的那几位：办不了
 * 出院，写不了病程、护理、体温单，查房选不到，页面也不提示。在院行数以床位数封顶（DRG 在院预警为同一理由去掉了
 * 上限），取全不会无界。
 *
 * `get` 传调用方自己的 `api`：管理端与医生端的 `api` 认证语义不同（见文件头），这里只管翻页、不碰请求本身。两边的
 * `api` 只回响应体、读不到响应头，所以不按 X-Total-Count 判，按「一页不满 500 条即取完」判——页长与后端上限是同一个数，
 * 后端上限若改小，一页不满就不再等于取完（`tests/test_inpatient_in_hospital_lists.py` 拿真接口跑这里，会红）。
 * 翻页之间恰有人入院，下一页会把上一页末行再给一遍：按 id 去重；一页里一条新的都没有就停，不会空转。
 */
async function fetchAllPages(get, path) {
  const PAGE = 500;
  const sep = path.includes("?") ? "&" : "?";
  const rows = new Map();
  for (let offset = 0; ; offset += PAGE) {
    const page = await get(`${path}${sep}limit=${PAGE}&offset=${offset}`);
    const before = rows.size;
    page.forEach((row) => { if (!rows.has(row.id)) rows.set(row.id, row); });
    if (page.length < PAGE || rows.size === before) return [...rows.values()];
  }
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
