/* 达妮娅工作台 · 页面状态与基础工具。
 *
 * 这里只放「跨视图共享」的东西：真实接口客户端、状态、外观偏好、主题、
 * 格式化与弹窗。业务动作在 studio.js，HTML 在 views.js。
 *
 * 关键约定：
 * - 数据全部来自后端；没有后端就进入明确错误态，不构造示例业务数据。
 * - 主题只读写 appearance.theme_override；不写 localStorage。follow 时跟随
 *   bridge.onContext / getContext 下发的面板主题。
 * - 外观偏好直接复用 classic/prefs.js，和经典界面共用同一份存储，避免双存储。
 */

import { api as defaultApi, assetUrl, renderOptions, OUTPUT_KEYS, boolValue } from "./api.js";
import { ACCENT_PRESETS, prefs, applyPrefs, loadPrefs } from "../classic/prefs.js";
import { createPlatformIcon } from "./platform-icons.js";

export { assetUrl, renderOptions, OUTPUT_KEYS, boolValue };

export const api = defaultApi;

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
export const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));

const iconPaths = {
  arrow: '<path d="M7 17 17 7M7 7h10v10"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2"/>',
  moon: '<path d="M20 14a8 8 0 0 1-10-10A8.5 8.5 0 1 0 20 14Z"/>',
  text: '<path d="M4 5h16M12 5v15m-4 0h8"/>',
  card: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 14h18m-7-6h4m-4 3h4"/><circle cx="8" cy="9" r="2"/>',
  media: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m10 8 5 4-5 4Z"/>',
  paste: '<path d="M8 5H5v16h14V5h-3"/><rect x="8" y="2" width="8" height="5" rx="2"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4"/>',
  copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 6a8 8 0 0 1 13 3M5 15a8 8 0 0 0 13 3"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
  warning: '<path d="M12 3 2.5 20h19L12 3Z"/><path d="M12 9v5m0 3v.5"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  play: '<path d="m9 5 10 7-10 7z"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="8.5" cy="9.5" r="1.5"/><path d="m21 15-5-5L5 21"/>',
  shield: '<path d="M12 3 4 6v6c0 5 3.4 8 8 9 4.6-1 8-4 8-9V6l-8-3Z"/><path d="m9 12 2 2 4-4"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
  sliders: '<path d="M4 6h10m4 0h2M4 12h4m4 0h8M4 18h12m4 0h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
};

export const icon = (name) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${iconPaths[name] || iconPaths.arrow}</svg>`;

/** 资源路径：以 studio 模块 URL 为基准，保留宿主的 asset_token。 */
export const asset = (name) => assetUrl(`./assets/${name}`);

export const names = {
  workbench: "工作台",
  library: "分享记录",
  studio: "卡片工坊",
  connections: "平台与账号",
  settings: "设置",
};

export const S = {
  view: "workbench",
  url: "",
  parseMode: "parse",
  busy: false,
  result: null,
  screenshot: null,
  error: "",
  records: [],
  thumbs: Object.create(null),
  thumbPending: new Set(),
  ov: null,
  config: null,
  values: {},
  library: null,
  outputs: { text: true, card: true, media: true },
  render: renderOptions({}),
  cardDraft: {},
  boardDark: false,
  sample: null,
  sampleBusy: false,
  sampleError: "",
  homeSample: null,
  homeSampleBusy: false,
  homeSampleError: "",
  head: "",
  headKind: "",
  badge: 0,
  version: "",
  connection: "",
  hostDark: false,
  booted: false,
  credentials: [],
  custom: null,
  cookiecloud: null,
  platformFilter: "all",
  prefs,
  accentPresets: ACCENT_PRESETS,
  compact: prefs.compact,
  animations: prefs.animations,
};

function syncPrefState() {
  S.prefs = prefs;
  S.compact = prefs.compact;
  S.animations = prefs.animations;
  S.accentPresets = ACCENT_PRESETS;
}

export function credentialsOf(platformName) {
  return (S.credentials || []).find((item) => item.id === platformName) || null;
}

export function status(credential) {
  if (!credential?.configured) return ["off", "未配置"];
  if (credential.status === "valid") return ["ok", "可用"];
  if (credential.status === "invalid") return ["off", "已失效"];
  return ["unknown", "已保存 · 待验证"];
}

export function date(value) {
  if (!value) return "尚未检查";
  const parsed = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  if (Number.isNaN(parsed.getTime())) return "刚刚";
  return parsed.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function ago(value) {
  if (!value) return "刚刚";
  const parsed = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  if (Number.isNaN(parsed.getTime())) return "刚刚";
  const minutes = Math.max(0, Math.floor((Date.now() - parsed.getTime()) / 60000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时前`;
  return date(value);
}

export const contentType = (value) => ({
  video: "视频",
  image_set: "图文",
  audio: "音乐",
  article: "文章",
}[value] || value || "内容");

export const layoutName = (key) => ({
  standard: "标准卡片",
  magazine: "杂志排版",
  immersive: "沉浸封面",
  feed: "信息流",
}[key] || "标准卡片");

export const intro = (eyebrow, title, sub, aside = "") =>
  `<section class="intro"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${sub}</p></div>${aside}</section>`;

export function outputNote() {
  if (!Object.values(S.outputs).some(Boolean)) return "三种输出都已关闭，机器人不会发送解析内容。";
  if (!S.outputs.card) return "分享卡片未选中；文本与富媒体按各自开关独立生效。";
  if (!S.render.enabled) return "卡片渲染总开关已关闭：即使选中分享卡片也不会发送卡片，请到卡片工坊开启。";
  return "文本、分享卡片与富媒体独立选择；卡片受「卡片渲染」总开关控制。";
}

/* ---------------- 外观偏好（与 classic 共用存储） ---------------- */

let prefQueue = Promise.resolve();

/** 立即应用，串行写入 appearance，失败回滚到调用前的快照。 */
export function savePrefs(patch, { onError } = {}) {
  const previous = {};
  for (const key of Object.keys(patch)) previous[key] = prefs[key];
  Object.assign(prefs, patch);
  syncPrefState();
  applyThemeFromPrefs();
  applyPrefs();
  refreshClassicAccents();
  const save = () => api.post("appearance", { prefs: { ...patch } });
  const operation = prefQueue.then(save, save);
  prefQueue = operation.catch((error) => {
    Object.assign(prefs, previous);
    syncPrefState();
    applyThemeFromPrefs();
    applyPrefs();
    refreshClassicAccents();
    if (onError) onError(error);
    else toast(`外观偏好保存失败：${error.message || error}`);
    try {
      globalThis.window?.dispatchEvent(new CustomEvent("denia:prefs-rollback"));
    } catch {
      /* 非浏览器环境忽略 */
    }
  });
  return operation;
}

export async function loadAppearance() {
  await loadPrefs(api);
  syncPrefState();
  applyThemeFromPrefs();
}

function bridgeObject() {
  const host = globalThis.window;
  return host?.AstrBotPluginPage || host?.AstrBotPluginView || null;
}

function hostIsDark(bridge) {
  try {
    if (typeof bridge?.getContext === "function") {
      const context = bridge.getContext();
      if (typeof context?.isDark === "boolean") return context.isDark;
    }
  } catch {
    /* 宿主上下文不可用时按当前页面主题，不编造 */
  }
  return document.documentElement.dataset.theme === "dark";
}

/** 把当前主题色同步到已挂载的 classic shadow 宿主。 */
export function refreshClassicAccents() {
  const rootStyle = getComputedStyle(document.documentElement);
  const accents = {};
  for (const name of ["--accent", "--accent-2", "--accent-soft", "--accent-ring", "--accent-text", "--bg-grad-a"]) {
    const value = rootStyle.getPropertyValue(name).trim();
    if (value) accents[name] = value;
  }
  for (const host of document.querySelectorAll(".classic-outlet")) {
    for (const [name, value] of Object.entries(accents)) host.style.setProperty(name, value);
  }
}

/** 把 appearance.theme_override 翻译成 <html data-theme>；follow 时读宿主。 */
export function applyThemeFromPrefs() {
  const root = document.documentElement;
  if (prefs.theme_override === "light" || prefs.theme_override === "dark") {
    root.dataset.theme = prefs.theme_override;
  } else {
    root.dataset.theme = S.hostDark ? "dark" : "light";
  }
}

export function initHostTheme() {
  const bridge = bridgeObject();
  S.hostDark = hostIsDark(bridge);
  applyThemeFromPrefs();
  if (typeof bridge?.onContext === "function") {
    try {
      bridge.onContext((context) => {
        if (typeof context?.isDark === "boolean") {
          S.hostDark = context.isDark;
          if (prefs.theme_override === "follow") applyThemeFromPrefs();
        } else {
          // 宿主可能直接改 <html data-theme> 而不传参数；follow 时以它为准。
          S.hostDark = document.documentElement.dataset.theme === "dark";
        }
        if (prefs.theme_override === "follow") applyPrefs();
        refreshClassicAccents();
        updateThemeButton();
      });
    } catch (error) {
      console.warn("[denia] 注册宿主主题回调失败", error);
    }
  }
  updateThemeButton();
}

export function updateThemeButton() {
  const button = $("#theme-button");
  if (!button) return;
  const dark = document.documentElement.dataset.theme === "dark";
  button.innerHTML = icon(dark ? "sun" : "moon");
  button.setAttribute("aria-label", dark ? "切换为浅色主题" : "切换为深色主题");
  button.title = prefs.theme_override === "follow" ? "跟随面板主题（点击改为固定主题）" : "切换主题";
}

/** 头部按钮：显式选择浅/深，写入 theme_override，不再是临时样式。 */
export function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  savePrefs({ theme_override: next }, { onError: (error) => toast(`主题保存失败：${error.message}`, "bad") });
  updateThemeButton();
  return next;
}

/* ---------------- 核心数据 ---------------- */

export async function loadCore() {
  const [overview, config, cache] = await Promise.all([
    api.get("overview"),
    api.get("config"),
    api.get("cache", { limit: 6, offset: 0 }),
  ]);
  if (!overview || !Array.isArray(overview.platforms)) throw new Error("总览接口没有返回平台列表");
  if (!config || !config.values) throw new Error("配置接口没有返回 values");
  if (!cache || !Array.isArray(cache.items)) throw new Error("分享记录接口没有返回列表");

  S.ov = overview;
  S.config = config;
  S.values = config.values || {};
  S.render = renderOptions(S.values);
  S.outputs = {
    text: boolValue(S.values[OUTPUT_KEYS.text]),
    card: boolValue(S.values[OUTPUT_KEYS.card]),
    media: boolValue(S.values[OUTPUT_KEYS.media]),
  };
  S.library = cache;
  S.records = cache.items;
  S.version = overview.version || "";
  S.booted = true;
  return { overview, config, cache };
}

/** 用后端保存后的 values 同步本地渲染状态与三个输出开关。 */
export function applyValues(values) {
  if (!values || typeof values !== "object") return;
  S.values = { ...S.values, ...values };
  S.render = renderOptions(S.values);
  S.outputs = {
    text: boolValue(S.values[OUTPUT_KEYS.text]),
    card: boolValue(S.values[OUTPUT_KEYS.card]),
    media: boolValue(S.values[OUTPUT_KEYS.media]),
  };
}

export async function reloadRecords(limit = 6) {
  const cache = await api.get("cache", { limit, offset: 0 });
  S.library = cache;
  S.records = Array.isArray(cache.items) ? cache.items : [];
  S.thumbs = Object.create(null);
  S.thumbPending.clear();
  return cache;
}

/* ---------------- 提示 / 弹窗 ---------------- */

let toastTimer = null;
export function toast(message) {
  const node = $("#toast");
  if (!node) return;
  node.textContent = message;
  node.dataset.kind = /失败|错误|不可|关闭/.test(message) ? "bad" : "ok";
  node.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("visible"), 3200);
}

let lastFocus = null;
let dialogTicket = 0;
export const isDialogCurrent = (ticket) => ticket === dialogTicket && Boolean($("#dialog")?.open);
export function dialog(title, body, actions = "") {
  lastFocus = document.activeElement;
  $("#dialog-content").innerHTML = `<div class="dialog-top"><h2 id="dialog-title">${title}</h2><button class="icon-button" data-action="close" aria-label="关闭弹窗">${icon("close")}</button></div>${body}${actions ? `<div class="dialog-actions">${actions}</div>` : ""}`;
  const node = $("#dialog");
  if (!node.open) node.showModal();
  return ++dialogTicket;
}

export const close = () => {
  dialogTicket += 1;
  const node = $("#dialog");
  if (node?.open) node.close();
};

export function initDialog() {
  const node = $("#dialog");
  if (!node) return;
  node.addEventListener("close", () => {
    dialogTicket += 1;
    if (lastFocus?.isConnected) lastFocus.focus();
  });
}

/* ---------------- 复用 classic 视图的宿主 ---------------- */

const CLASSIC_CSS_URL = assetUrl("../classic/style.css");
let classicCssPromise = null;

function loadClassicCss() {
  if (!classicCssPromise) {
    classicCssPromise = fetch(CLASSIC_CSS_URL, { credentials: "same-origin" })
      .then((response) => {
        if (!response.ok) throw new Error(`经典样式载入失败（HTTP ${response.status}）`);
        return response.text();
      })
      .catch((error) => {
        classicCssPromise = null;
        throw error;
      });
  }
  return classicCssPromise;
}

/** Shadow DOM 里的 classic 布局与配色适配。
 *
 * classic/style.css 的 :root 变量不会命中 shadow root，而自定义属性可以继承；
 * 但 classic 的变量名和新版不同（--panel/--text vs --surface/--ink），所以这里
 * 显式给一套 light/dark 变量。主题色仍由 documentElement 上的 --accent 供入：
 * classicOutlet 会把当前 --accent 系列写到宿主元素的行内样式，优先于这里的默认值。
 */
const CLASSIC_SHADOW_PATCH = `
:host {
  display:block; color:var(--text);
  font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:13px; line-height:1.6;
  --bg:#eef1f6; --bg-grad-a:rgba(47,111,221,.06); --bg-grad-b:rgba(139,124,246,.05);
  --panel:#ffffff; --panel-solid:#ffffff; --panel-2:rgba(20,32,54,.045);
  --panel-border:rgba(20,32,54,.09); --text:#171d28; --text-2:#4d5869; --text-3:#8792a2;
  --border:#dde3ec; --border-2:#c6cfdb; --accent:#2f6fdd; --accent-2:#7a5cff;
  --accent-text:#ffffff; --accent-soft:rgba(47,111,221,.12); --accent-ring:rgba(47,111,221,.35);
  --ok:#0e9f6e; --ok-soft:rgba(14,159,110,.12); --warn:#b7791f; --warn-soft:rgba(183,121,31,.12);
  --err:#dc4446; --err-soft:rgba(220,68,70,.12);
  --radius:14px; --radius-sm:9px; --radius-lg:20px; --pad:16px; --gap:14px;
  --shadow-1:0 1px 3px rgba(15,23,42,.035); --shadow-2:0 12px 40px -12px rgba(15,23,42,.25);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --ease:cubic-bezier(.22,.68,.36,1); --dur:.18s;
}
:host-context([data-theme="dark"]) {
  --bg:#0d1017; --bg-grad-a:rgba(110,166,245,.08); --bg-grad-b:rgba(154,124,255,.07);
  --panel:#181d27; --panel-solid:#181d27; --panel-2:rgba(255,255,255,.05);
  --panel-border:rgba(255,255,255,.09); --text:#e9edf4; --text-2:#a4aebe; --text-3:#75808f;
  --border:#262d3a; --border-2:#384252; --accent:#6ea6f5; --accent-2:#9a7cff;
  --accent-text:#0d1420; --accent-soft:rgba(110,166,245,.16); --accent-ring:rgba(110,166,245,.4);
  --ok:#4ecb8f; --ok-soft:rgba(78,203,143,.14); --warn:#e5b567; --warn-soft:rgba(229,181,103,.14);
  --err:#f28b85; --err-soft:rgba(242,139,133,.14);
  --shadow-1:0 1px 3px rgba(0,0,0,.15); --shadow-2:0 16px 48px -12px rgba(0,0,0,.6);
}
:host-context([data-radius="s"]) { --radius:9px; --radius-sm:6px; --radius-lg:13px; }
:host-context([data-radius="l"]) { --radius:19px; --radius-sm:12px; --radius-lg:26px; }
:host-context([data-compact="1"]) { --pad:11px; --gap:9px; }
:host-context([data-motion="0"]) * { transition:none!important; animation:none!important; }
.app { display:block; height:auto; min-height:0; }
.stage { display:block; height:auto; overflow:visible; }
.stage-body { display:block; height:auto; overflow:visible; padding:0; }
.view { max-width:none; }
 .bg-aurora { display:none; }
:host {
  min-width:0; max-width:100%;
  --panel:var(--surface); --panel-solid:var(--surface); --panel-2:var(--tint);
  --text:var(--ink); --text-2:var(--sub); --text-3:var(--sub);
  --border:var(--line); --border-2:var(--line); --panel-border:var(--line);
  --accent:var(--green); --accent-2:var(--green); --accent-soft:var(--tint);
  --accent-text:var(--lime-ink);
}
.stage-body,.view,.card,.card-body { min-width:0; max-width:100%; }
.field-body > div { min-width:0; max-width:100%; width:100%; }
.pick-grid { width:100%; max-width:100%; grid-template-columns:repeat(auto-fit,minmax(min(148px,100%),1fr)); }
.field-label code { overflow-wrap:anywhere; max-width:100%; }
:host { --pad:12px; --gap:10px; }
.card { padding:12px; }
.card-head { margin-bottom:8px; }
.field { padding:10px 0; }
.row { padding:9px 0; }
.cred-guide { margin:8px 0; border-top:1px solid var(--line); padding-top:7px; }
.cred-guide summary { cursor:pointer; color:var(--green); font-size:12px; }
.cred-guide .field-hint { padding-top:7px; }
.cfg-group-head { margin-bottom:0; }
.card:has(>.cfg-group-head:only-child) { padding-block:10px; }
:host(#connections-credentials) { font-size:14px; line-height:1.7; }
:host(#connections-credentials) .card-title { font-size:16px; }
:host(#connections-credentials) .card-note,
:host(#connections-credentials) .field-hint,
:host(#connections-credentials) .cred-meta,
:host(#connections-credentials) .cred-guide summary { font-size:13px; line-height:1.7; }
:host(#connections-credentials) .btn { font-size:13px; }
:host(#connections-credentials) .pill { font-size:12px; }
:host(#connections-credentials) .field-label { font-size:14px; }
:host(#connections-credentials) textarea { font-size:14px; line-height:1.6; }
:host(#connections-credentials) .cred-tabs { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; background:none; padding:0; border:0; }
:host(#connections-credentials) .cred-tabs button { display:flex; align-items:center; justify-content:flex-start; gap:9px; min-width:0; padding:10px; border:1px solid var(--line); border-radius:10px; text-align:left; white-space:normal; font-size:14px; line-height:1.5; }
:host(#connections-credentials) .cred-tabs button.is-active { background:var(--tint); border-color:var(--green); box-shadow:none; }
:host(#connections-credentials) .platform-icon { flex:none; box-sizing:content-box; width:24px; height:24px; padding:5px; background:#fff; border:1px solid #16121c12; border-radius:9px; object-fit:contain; }
:host(#connections-credentials) .cred-tab-copy { display:flex; flex-direction:column; min-width:0; gap:1px; }
:host(#connections-credentials) .cred-tab-label { font-weight:600; color:var(--ink); }
:host(#connections-credentials) .cred-tab-state { color:var(--sub); font-size:12px; }
`;

let shadowSeq = 0;

/** 在宿主节点里建 shadow root，并载入 classic/style.css。
 *
 * 用 shadow root 而不是整页 iframe：业务组件仍是同一个模块、同一个 ctx、
 * 同一个事件循环，只是样式被隔离，不会把新版页面改成经典外观。
 */
export async function classicOutlet(hostElement) {
  if (!hostElement) throw new Error("缺少 classic 视图容器");
  let shadow = hostElement.shadowRoot;
  if (!shadow) {
    shadow = hostElement.attachShadow({ mode: "open" });
    hostElement.dataset.outlet = `classic-${++shadowSeq}`;
    const style = document.createElement("style");
    style.dataset.deniaClassicCss = "1";
    try {
      const css = (await loadClassicCss()).replace(/^@charset[^;]+;/i, "");
      style.textContent = `${css}\n${CLASSIC_SHADOW_PATCH}`;
    } catch (error) {
      console.warn("[denia] 经典样式载入失败，业务组件仍可用", error);
      style.textContent = CLASSIC_SHADOW_PATCH;
    }
    shadow.appendChild(style);
    // 主题色由 <html> 上的 --accent 系列决定；写到宿主行内样式，穿透到 shadow。
    const rootStyle = getComputedStyle(document.documentElement);
    for (const name of ["--accent", "--accent-2", "--accent-soft", "--accent-ring", "--accent-text", "--bg-grad-a"]) {
      const value = rootStyle.getPropertyValue(name).trim();
      if (value) hostElement.style.setProperty(name, value);
    }
    const app = document.createElement("div");
    app.className = "app";
    const stage = document.createElement("main");
    stage.className = "stage";
    const body = document.createElement("div");
    body.className = "stage-body";
    const view = document.createElement("div");
    view.className = "view";
    body.appendChild(view);
    stage.appendChild(body);
    app.appendChild(stage);
    shadow.appendChild(app);
  }
  return {
    shadow,
    view: shadow.querySelector(".stage-body .view"),
    body: shadow.querySelector(".stage-body"),
  };
}

/** 挂载一个 classic 视图工厂，返回它自己的实例（含 unmount）。 */
export async function mountClassicView(hostElement, factory, ctx) {
  const outlet = await classicOutlet(hostElement);
  const instance = factory(ctx);
  await instance.mount(outlet.view);
  return { instance, outlet };
}

/** 挂载 classic 组件节点（凭据 / CookieCloud / 诊断卡）。 */
export async function mountClassicNodes(hostElement, nodes) {
  const outlet = await classicOutlet(hostElement);
  for (const node of nodes) {
    if (node) outlet.view.appendChild(node);
  }
  return outlet;
}

export function clearClassic(hostElement) {
  const shadow = hostElement?.shadowRoot;
  const view = shadow?.querySelector(".stage-body .view");
  if (view) view.replaceChildren();
}

/* ---------------- 视图上下文（给 classic 工厂） ---------------- */

export function createContext(overrides = {}) {
  return {
    api,
    prefs,
    createPlatformIcon,
    accentPresets: ACCENT_PRESETS,
    applyPrefs,
    savePrefs: (patch) => savePrefs(patch, { onError: (error) => toast(`外观偏好保存失败：${error.message}`) }),
    setHead(text, kind = "") {
      S.head = text || "";
      S.headKind = kind || "";
      const node = $("#head-status");
      if (node) {
        node.hidden = !text;
        node.textContent = text || "";
        node.className = `status ${kind}`.trim();
      }
    },
    setBadge(count) {
      S.badge = Number(count) || 0;
      const node = $("#nav-badge");
      if (node) {
        node.hidden = S.badge <= 0;
        node.textContent = S.badge > 999 ? "999+" : String(S.badge);
      }
    },
    setVersion(text) {
      if (text) S.version = String(text).replace(/^v/i, "");
      const node = $("[data-version]");
      if (node) node.textContent = S.version ? `v${S.version}` : "";
    },
    setConnection(ok, text) {
      S.connection = text || "";
      const node = $("[data-connection]");
      if (node) {
        node.textContent = text || "";
        node.dataset.ok = ok ? "1" : "0";
      }
    },
    goTo(view) {
      if (names[view]) location.hash = `/${view}`;
    },
    ...overrides,
  };
}
