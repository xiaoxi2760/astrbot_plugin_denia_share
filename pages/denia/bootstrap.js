/* 希望解析器 · 管理页面启动器
 *
 * 这个文件只有一个职责：在宿主 bridge 就绪后读取 WEBUI_STYLE，按需加载
 * 「经典界面」或「达妮娅的泡泡工坊」。它不导入 app.js / studio.js 的
 * 任何业务模块，也不写配置；界面模块本身仍保持原来的启动方式。
 *
 * 失败策略：
 *   - ?ui=classic 是临时恢复入口：完全不读配置，也不改写已保存的设置；
 *   - 配置读取失败、值非法、新版模板/样式/模块加载失败 → 回退 classic/index.html；
 *   - 经典界面也失败时才显示致命提示。
 *
 * 注意：两处 import("./classic/app.js") / import("./studio/studio.js") 必须保持字面量。
 * 保留字面量路径以兼容宿主的资源处理；当前宿主使用路径 token，
 * 嵌套 CSS 与 ES 模块通过相对 URL 自然继承同一页面的资源权限。
 */

const STYLE_CLASSIC = "classic";
const STYLE_DENIA = "denia";
const WEBUI_STYLE_KEY = "WEBUI_STYLE";
const API_PREFIX = "/api/plug/astrbot_plugin_denia_share";
const BRIDGE_READY_TIMEOUT_MS = 8000;
const CONFIG_TIMEOUT_MS = 8000;
const TEMPLATE_TIMEOUT_MS = 10000;
const STYLE_TIMEOUT_MS = 10000;

/** 两种界面各自的模板与入口模块；启动器也以这份表为准。 */
export const STYLE_TEMPLATES = Object.freeze({
  classic: Object.freeze({ template: "./classic/index.html", module: "./classic/app.js", label: "经典界面" }),
  denia: Object.freeze({ template: "./studio/index.html", module: "./studio/studio.js", label: "达妮娅的泡泡工坊" }),
});

/** 把配置值收敛成启动器认识的两个字面量；非法值返回 null。 */
export function normalizeStyleValue(raw) {
  if (typeof raw !== "string") return null;
  const value = raw.trim().toLowerCase();
  return value === STYLE_CLASSIC || value === STYLE_DENIA ? value : null;
}

/** ?ui=classic 临时覆盖；只认 classic，其他 ui 值一律忽略。 */
export function styleOverrideFromSearch(search = "") {
  const value = new URLSearchParams(search).get("ui");
  return value && value.trim().toLowerCase() === STYLE_CLASSIC ? STYLE_CLASSIC : null;
}

/** 生成/保留当前页面所有查询参数与 hash 的经典恢复地址。 */
export function classicRecoveryUrl(href) {
  const url = new URL(href);
  url.searchParams.set("ui", STYLE_CLASSIC);
  return url.toString();
}

/** 去掉临时覆盖，回到已保存的界面设置。 */
export function withoutClassicOverrideUrl(href) {
  const url = new URL(href);
  url.searchParams.delete("ui");
  return url.toString();
}

/**
 * 模板/资源 URL：相对 import.meta.url 解析，并保留模块 URL 上的查询串
 * （宿主下发的 asset_token、theme 等都在这里）。Hash 不参与资源请求。
 */
export function templateUrl(relative, moduleHref = import.meta.url) {
  const base = new URL(moduleHref);
  const url = new URL(relative, base);
  if (base.search) url.search = base.search;
  url.hash = "";
  return url;
}

/** 开发态配置接口；与 app.js 的既有约定完全一致（含 ?api= 覆盖）。 */
export function configEndpoint(search = "") {
  const override = new URLSearchParams(search).get("api");
  const base = override
    ? `${override.replace(/\/+$/, "")}${API_PREFIX}`
    : API_PREFIX;
  return `${base}/config`;
}

/** 宿主可能用新名字 AstrBotPluginView；经典 app.js 只认旧名字，这里做一次兼容别名。 */
export async function waitForBridge({
  windowObject = globalThis.window,
  timeoutMs = BRIDGE_READY_TIMEOUT_MS,
} = {}) {
  const bridge = windowObject?.AstrBotPluginPage || windowObject?.AstrBotPluginView || null;
  if (!bridge) return null;
  if (!windowObject.AstrBotPluginPage) {
    try {
      windowObject.AstrBotPluginPage = bridge;
    } catch {
      /* 只读属性时忽略：调用方仍可继续用返回的 bridge */
    }
  }
  if (typeof bridge.ready === "function") {
    try {
      await withTimeout(Promise.resolve(bridge.ready()), timeoutMs, "等待宿主 bridge");
    } catch (error) {
      // 与 app.js 的既有行为一致：ready 失败不阻断页面，后面读配置再报错。
      console.warn("[denia] bridge.ready() 失败，继续尝试读取配置", error);
    }
  }
  return bridge;
}

/**
 * 读取配置值。bridge 存在时走宿主 apiGet；没有 bridge 时走现有
 * /api/plug/astrbot_plugin_denia_share/config，并支持 ?api= 覆盖。
 * 两条路径共用 extractConfigValues 的错误判据。
 */
export async function readConfigValues({
  bridge = null,
  fetchImpl = globalThis.fetch,
  search = "",
  origin = globalThis.location?.origin || "",
  timeoutMs = CONFIG_TIMEOUT_MS,
} = {}) {
  if (bridge && typeof bridge.apiGet === "function") {
    const payload = await withTimeout(
      Promise.resolve(bridge.apiGet("config")),
      timeoutMs,
      "读取界面设置",
    );
    return extractConfigValues(payload);
  }

  if (typeof fetchImpl !== "function") {
    throw new Error("当前环境没有可用的配置接口");
  }
  const endpoint = configEndpoint(search);
  const base = origin || globalThis.location?.origin || "http://localhost";
  const url = /^https?:\/\//i.test(endpoint) ? endpoint : new URL(endpoint, base).toString();
  const response = await withTimeout(
    Promise.resolve(
      fetchImpl(url, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      }),
    ),
    timeoutMs,
    "读取界面设置",
  );

  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const message = (body && body.message) || "";
  if (body && body.status === "error") {
    throw new Error(message || `读取界面设置失败（HTTP ${response.status}）`);
  }
  if (!response.ok) {
    throw new Error(message || `读取界面设置失败（HTTP ${response.status}）`);
  }
  return extractConfigValues(body);
}

/** 统一解包：bridge 可能已拆信封，开发态可能是裸对象；错误依据只有 status/HTTP。 */
function extractConfigValues(payload) {
  let body = payload;
  if (body && body.status === "error") {
    throw new Error(body.message || "读取界面设置失败");
  }
  if (body && body.status === "ok" && body.data && typeof body.data === "object") {
    body = body.data;
  }
  if (!body || typeof body !== "object" || !body.values || typeof body.values !== "object") {
    throw new Error("配置接口没有返回 values");
  }
  return body.values;
}

export async function resolveStyleChoice({
  bridge = null,
  fetchImpl = globalThis.fetch,
  search = "",
  origin = globalThis.location?.origin || "",
  timeoutMs = CONFIG_TIMEOUT_MS,
} = {}) {
  if (styleOverrideFromSearch(search)) {
    return { style: STYLE_CLASSIC, source: "override" };
  }

  let values;
  try {
    values = await readConfigValues({ bridge, fetchImpl, search, origin, timeoutMs });
  } catch (error) {
    return { style: STYLE_CLASSIC, source: "error", error };
  }

  const raw = values[WEBUI_STYLE_KEY];
  if (raw === undefined || raw === null || String(raw).trim() === "") {
    return { style: STYLE_CLASSIC, source: "default" };
  }
  const style = normalizeStyleValue(raw);
  if (!style) {
    return { style: STYLE_CLASSIC, source: "invalid", value: raw };
  }
  return { style, source: "config" };
}

/** 默认模块加载器：两个 import 必须是字面量，宿主才能重写并带上 asset_token。 */
export function defaultImport(style) {
  return style === STYLE_DENIA ? import("./studio/studio.js") : import("./classic/app.js");
}

export async function boot({
  windowObject = globalThis.window,
  documentObject = globalThis.document,
  fetchImpl = globalThis.fetch,
  importModule = defaultImport,
  moduleHref = import.meta.url,
} = {}) {
  if (!documentObject) throw new Error("没有可用的 document");

  const locationObject = windowObject?.location;
  const search = locationObject?.search || "";
  const href = locationObject?.href || "";
  const origin = locationObject?.origin || "";
  const recovery = documentObject.getElementById("denia-boot-recovery");
  if (recovery && href) recovery.setAttribute("href", classicRecoveryUrl(href));
  // The bridge can deliver its first theme while ready() is pending. Capture it
  // afterwards so a failed preview cannot leave classic with the preview's theme.
  let bridge = null;
  try {
    bridge = await waitForBridge({ windowObject });
  } catch (error) {
    console.warn("[denia] 等待 bridge 失败，继续读取配置", error);
  }
  const hostContext = typeof bridge?.getContext === "function" ? bridge.getContext() : null;
  const initialTheme = typeof hostContext?.isDark === "boolean"
    ? (hostContext.isDark ? "dark" : "light")
    : documentObject.documentElement?.dataset?.theme || "";

  if (styleOverrideFromSearch(search)) {
    await showClassic({
      documentObject,
      fetchImpl,
      importModule,
      moduleHref,
      href,
      initialTheme,
      notice: {
        kind: "info",
        title: "临时使用经典界面",
        text: "?ui=classic 只对本次打开生效，不会改写已保存的界面设置。",
        action: {
          href: withoutClassicOverrideUrl(href),
          label: "按已保存的设置打开",
        },
      },
    });
    return;
  }

  const choice = await resolveStyleChoice({ bridge, fetchImpl, search, origin });

  if (choice.style === STYLE_DENIA) {
    try {
      await showDenia({ documentObject, fetchImpl, importModule, moduleHref, href });
      return;
    } catch (error) {
      console.error("[denia] 达妮娅的泡泡工坊加载失败，回退经典界面", error);
      await showClassic({
        documentObject,
        fetchImpl,
        importModule,
        moduleHref,
        href,
        initialTheme,
        notice: {
          kind: "warn",
          title: "达妮娅的泡泡工坊未能载入，已切回经典界面",
          text: `${describeError(error)}。已保存的界面设置没有被改写，可在原生配置的「界面设置」中重新选择。`,
        },
      });
      return;
    }
  }

  let notice = null;
  if (choice.source === "error") {
    notice = {
      kind: "warn",
      title: "无法读取界面设置，已使用经典界面",
      text: `${describeError(choice.error)}。可在原生配置的「界面设置」中检查后再刷新。`,
    };
  } else if (choice.source === "invalid") {
    notice = {
      kind: "warn",
      title: "界面设置值无效，已使用经典界面",
      text: `WEBUI_STYLE 只能是 classic 或 denia，当前值：${safeValue(choice.value)}。可在原生配置的「界面设置」中重新选择。`,
    };
  }

  await showClassic({
    documentObject,
    fetchImpl,
    importModule,
    moduleHref,
    href,
    initialTheme,
    notice,
  });
}

async function showClassic({
  documentObject,
  fetchImpl,
  importModule,
  moduleHref,
  href,
  initialTheme = "",
  notice,
}) {
  removeStyles(documentObject, STYLE_DENIA);
  const parsed = await fetchTemplate(STYLE_TEMPLATES.classic.template, { fetchImpl, moduleHref });
  await loadStyles(parsed, documentObject, STYLE_CLASSIC);
  installTemplate(parsed, documentObject, initialTheme);
  await importModule(STYLE_CLASSIC);
  if (notice) showNotice(documentObject, notice);
}

async function showDenia({ documentObject, fetchImpl, importModule, moduleHref, href }) {
  const parsed = await fetchTemplate(STYLE_TEMPLATES.denia.template, { fetchImpl, moduleHref });
  await loadStyles(parsed, documentObject, STYLE_DENIA);
  installTemplate(parsed, documentObject);

  // 不依赖 studio.js 成功执行：它若在绑定事件前崩溃，这个链接仍然可用。
  documentObject.querySelectorAll("[data-open-classic]").forEach((anchor) => {
    anchor.setAttribute("href", classicRecoveryUrl(href));
  });

  await importModule(STYLE_DENIA);
}

export async function fetchTemplate(relative, { fetchImpl, moduleHref }) {
  if (typeof fetchImpl !== "function") throw new Error("当前环境没有可用的模板请求");
  const url = templateUrl(relative, moduleHref);
  const response = await withTimeout(
    Promise.resolve(
      fetchImpl(url.toString(), {
        headers: { Accept: "text/html" },
        credentials: "same-origin",
      }),
    ),
    TEMPLATE_TIMEOUT_MS,
    `载入 ${relative}`,
  );
  if (!response.ok) {
    throw new Error(`载入 ${relative} 失败（HTTP ${response.status}）`);
  }
  const html = await response.text();
  if (!html || !html.trim()) throw new Error(`载入 ${relative} 失败（内容为空）`);

  const parsed = new DOMParser().parseFromString(html, "text/html");
  if (!parsed || !parsed.body) throw new Error(`解析 ${relative} 失败`);

  // 模板里的 script 只对「直接打开 classic.html」有意义；启动器自己 import，
  // 不能让 DOMParser 生成的脚本或桥接脚本再执行一次。
  parsed.body.querySelectorAll("script").forEach((script) => script.remove());

  // The shell is mounted into the stable root document. Resolve its resources
  // against the shell URL first; CSS/ES module imports then resolve naturally
  // within classic/ or studio/, including AstrBot's path-token prefix.
  for (const element of parsed.querySelectorAll("[src], [href], [poster]")) {
    for (const attribute of ["src", "href", "poster"]) {
      const value = element.getAttribute(attribute);
      if (!value || /^(?:#|[a-z][a-z0-9+.-]*:|\/\/)/i.test(value)) continue;
      const resolved = new URL(value, url);
      if (resolved.origin === url.origin && !resolved.search) resolved.search = url.search;
      element.setAttribute(attribute, resolved.href);
    }
  }

  const styles = [...parsed.querySelectorAll('link[rel~="stylesheet"][href]')].map((link) => ({
    href: link.getAttribute("href"),
    media: link.getAttribute("media") || "",
  }));

  return {
    title: parsed.title || "",
    theme: parsed.documentElement.getAttribute("data-theme") || "",
    styles,
    nodes: [...parsed.body.childNodes],
    bodyClass: parsed.body.className || "",
  };
}

async function loadStyles(parsed, documentObject, styleName) {
  if (!documentObject.head) throw new Error("页面缺少 head，无法载入样式");
  const pending = [];
  for (const style of parsed.styles) {
    if (!style.href) continue;
    const link = documentObject.createElement("link");
    link.rel = "stylesheet";
    link.href = style.href;
    if (style.media) link.media = style.media;
    link.dataset.deniaStyle = styleName;

    pending.push(
      new Promise((resolve, reject) => {
        const timer = setTimeout(
          () => reject(new Error(`样式载入超时：${style.href}`)),
          STYLE_TIMEOUT_MS,
        );
        link.addEventListener(
          "load",
          () => {
            clearTimeout(timer);
            resolve();
          },
          { once: true },
        );
        link.addEventListener(
          "error",
          () => {
            clearTimeout(timer);
            reject(new Error(`样式载入失败：${style.href}`));
          },
          { once: true },
        );
      }),
    );
    documentObject.head.appendChild(link);
  }
  await Promise.all(pending);
}

function installTemplate(parsed, documentObject, fallbackTheme = "") {
  const body = documentObject.body;
  if (!body) throw new Error("页面缺少 body，无法载入界面");

  // denia 壳自带 data-theme；经典模板没有时保留进入启动器前的宿主主题，
  // 避免从预览回退后把亮色/暗色状态带错。
  if (parsed.theme) documentObject.documentElement.dataset.theme = parsed.theme;
  else if (fallbackTheme) documentObject.documentElement.dataset.theme = fallbackTheme;
  body.className = parsed.bodyClass;
  while (body.firstChild) body.removeChild(body.firstChild);
  for (const node of parsed.nodes) body.appendChild(node);
  if (parsed.title) documentObject.title = parsed.title;
}

function removeStyles(documentObject, styleName) {
  documentObject
    .querySelectorAll(`link[data-denia-style="${styleName}"]`)
    .forEach((node) => node.remove());
}

function showNotice(documentObject, spec) {
  if (!documentObject?.body) return;
  const existing = documentObject.getElementById("denia-boot-notice");
  if (existing) existing.remove();

  const aside = documentObject.createElement("aside");
  aside.id = "denia-boot-notice";
  aside.className = "denia-boot-notice";
  aside.dataset.kind = spec.kind || "info";
  aside.setAttribute("role", "status");
  aside.setAttribute("aria-live", "polite");

  const copy = documentObject.createElement("div");
  copy.className = "denia-boot-notice-copy";
  const title = documentObject.createElement("strong");
  title.textContent = spec.title || "";
  const text = documentObject.createElement("span");
  text.textContent = spec.text || "";
  copy.append(title, text);

  const actions = documentObject.createElement("div");
  actions.className = "denia-boot-notice-actions";
  if (spec.action && spec.action.href) {
    const anchor = documentObject.createElement("a");
    anchor.setAttribute("href", spec.action.href);
    anchor.textContent = spec.action.label || "打开";
    actions.appendChild(anchor);
  }
  const close = documentObject.createElement("button");
  close.type = "button";
  close.textContent = "关闭";
  close.addEventListener("click", () => aside.remove());
  actions.appendChild(close);

  aside.append(copy, actions);
  documentObject.body.appendChild(aside);
}

/** 致命错误（经典界面也无法启动）时保留一条可见说明，避免整页空白。 */
export function showFatal(
  error,
  { documentObject = globalThis.document } = {},
) {
  try {
    console.error("[denia] 管理页面启动失败", error);
    const message = describeError(error);
    const boot = documentObject?.getElementById("denia-boot");
    if (boot) {
      while (boot.firstChild) boot.removeChild(boot.firstChild);
      const card = documentObject.createElement("div");
      card.className = "denia-boot-card";
      const mark = documentObject.createElement("span");
      mark.className = "denia-boot-mark";
      mark.setAttribute("aria-hidden", "true");
      const title = documentObject.createElement("p");
      title.className = "denia-boot-title";
      title.textContent = "管理页面未能载入";
      const text = documentObject.createElement("p");
      text.className = "denia-boot-text";
      text.textContent = `${message}。请刷新页面；若仍然失败，请在原生配置的「界面设置」中选择经典界面。`;
      card.append(mark, title, text);
      boot.appendChild(card);
      return;
    }
    showNotice(
      documentObject,
      {
        kind: "warn",
        title: "管理页面未能载入",
        text: `${message}。请刷新页面；若仍然失败，请在原生配置的「界面设置」中选择经典界面。`,
      },
    );
  } catch (fatalError) {
    console.error("[denia] 连错误提示都无法渲染", fatalError);
  }
}

function withTimeout(promise, timeoutMs, label) {
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}超时`)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function describeError(error) {
  const text = (error && (error.message || String(error))) || "未知错误";
  return text.length > 240 ? `${text.slice(0, 240)}…` : text;
}

function safeValue(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return (text || "").slice(0, 80);
}

if (!globalThis.__DENIA_BOOT_NO_AUTOBOOT__) {
  boot().catch((error) => showFatal(error));
}
