/* 达妮娅工作台的接口层。
 *
 * 与 classic/app.js 的 api 封装保持同一条契约：
 * - 有宿主的 AstrBotPluginPage bridge 就全部走 bridge（鉴权与页面 token 由宿主处理）；
 * - 没有 bridge（直接打开 studio-shell.html 或本地静态服务器）时直连
 *   /api/plug/astrbot_plugin_denia_share，支持 ?api=http://host:port 覆盖；
 * - 成功响应是裸业务对象；失败可能是 {status:"error", message} 或非 2xx；
 *   两条路径都必须抛出同一条可展示的错误，绝不把错误页/空体当业务数据。
 * - 下载统一走 api.download：bridge 用宿主 download，开发态自己取 blob 落盘。
 *
 * 本模块不读 localStorage、不碰凭据，只做请求与解包；UI 状态在 core.js。
 */

export const API_PREFIX = "/api/plug/astrbot_plugin_denia_share";
export const PLUGIN_NAME = "astrbot_plugin_denia_share";

/** 后端真实卡片设计枚举（见 core/card_renderer.py LAYOUT_NAMES / theme）。 */
export const CARD_LAYOUTS = ["standard", "magazine", "immersive", "feed"];
export const CARD_THEMES = ["dark", "light"];
export const CARD_LAYOUT_LABELS = {
  standard: "标准卡片",
  magazine: "杂志排版",
  immersive: "沉浸封面",
  feed: "信息流",
};

/** 卡片配置键 → preview/sample 的外观参数名。这份映射必须与后端一致。 */
export const PREVIEW_OVERRIDES = {
  RENDER_THEME: "theme",
  RENDER_LAYOUT: "layout",
  RENDER_WIDTH: "width",
  RENDER_ACCENT_COLOR: "accent_color",
  RENDER_WATERMARK: "watermark",
  RENDER_DESC_MAX_LINES: "desc_max_lines",
  RENDER_SHOW_AVATAR: "show_avatar",
  RENDER_SHOW_PLAY_BUTTON: "show_play_button",
  RENDER_SHOW_AI_SUMMARY: "show_ai_summary",
  RENDER_COVER_FULL_SIZE: "cover_full_size",
  RENDER_GRADIENT_TOP: "gradient_top",
  RENDER_GRADIENT_BOTTOM: "gradient_bottom",
};

/** 输出开关的配置键，与聊天链路一致。 */
export const OUTPUT_KEYS = {
  text: "OUTPUT_TEXT_ENABLED",
  card: "OUTPUT_CARD_ENABLED",
  media: "OUTPUT_MEDIA_ENABLED",
};

const TRUE_TEXT = new Set(["true", "1", "yes", "on"]);

/** 后端/宿主可能给 bool、0/1 或字符串；统一收敛成布尔。 */
export function boolValue(value) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return TRUE_TEXT.has(value.trim().toLowerCase());
  return false;
}

/** 从配置 values 还原真实的渲染开关与外观。 */
export function renderOptions(values = {}) {
  const layout = CARD_LAYOUTS.includes(String(values.RENDER_LAYOUT))
    ? String(values.RENDER_LAYOUT)
    : "standard";
  const theme = CARD_THEMES.includes(String(values.RENDER_THEME))
    ? String(values.RENDER_THEME)
    : "dark";
  const width = Number(values.RENDER_WIDTH);
  return {
    enabled: boolValue(values.RENDER_ENABLED),
    theme,
    layout,
    width: Number.isFinite(width) ? Math.max(520, Math.min(1080, Math.round(width))) : 720,
    accent_color: typeof values.RENDER_ACCENT_COLOR === "string" ? values.RENDER_ACCENT_COLOR : "",
    watermark: typeof values.RENDER_WATERMARK === "string" ? values.RENDER_WATERMARK : "",
    desc_max_lines: Number.isFinite(Number(values.RENDER_DESC_MAX_LINES))
      ? Math.max(0, Math.min(12, Math.round(Number(values.RENDER_DESC_MAX_LINES))))
      : 4,
    show_avatar: boolValue(values.RENDER_SHOW_AVATAR),
    show_play_button: boolValue(values.RENDER_SHOW_PLAY_BUTTON),
    show_ai_summary: boolValue(values.RENDER_SHOW_AI_SUMMARY),
    cover_full_size: boolValue(values.RENDER_COVER_FULL_SIZE),
    gradient_top: typeof values.RENDER_GRADIENT_TOP === "string" ? values.RENDER_GRADIENT_TOP : "",
    gradient_bottom: typeof values.RENDER_GRADIENT_BOTTOM === "string" ? values.RENDER_GRADIENT_BOTTOM : "",
  };
}

/** 把当前编辑值翻成 preview/sample 的 preview 对象。
 *
 * 颜色/水印允许空串（表示「用默认」），所以这里保留空值而不是丢弃；
 * 沉浸式布局不允许 AI 摘要，明确下发 false，和服务端契约一致。
 */
export function buildPreviewOverrides(values = {}) {
  const overrides = {};
  for (const [key, name] of Object.entries(PREVIEW_OVERRIDES)) {
    if (!Object.prototype.hasOwnProperty.call(values, key)) continue;
    overrides[name] = values[key];
  }
  if (overrides.layout === "immersive") overrides.show_ai_summary = false;
  return overrides;
}

/** 把响应体收敛成裸业务对象；错误信封抛出可展示的 Error。
 *
 * bridge 可能在旧版本里套 {status:"ok", data}，开发态是裸对象，两种都接受。
 */
export function unwrapPayload(body) {
  if (body === null || body === undefined) {
    throw new Error("接口没有返回数据");
  }
  if (body && typeof body === "object" && body.status === "error") {
    throw new Error(body.message || "请求失败");
  }
  if (body && typeof body === "object" && body.status === "ok" && body.data && typeof body.data === "object") {
    return body.data;
  }
  return body;
}

/** 解包 HTTP 响应；非 2xx 且没有错误信封时也不能把错误页交给视图。 */
export async function unwrapResponse(response) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const message = (body && body.message) || "";
  if (body && body.status === "error") {
    throw new Error(message || `请求失败（HTTP ${response.status}）`);
  }
  if (!response.ok) {
    throw new Error(message || `请求失败（HTTP ${response.status}）`);
  }
  return body;
}

/** 开发态后端根地址；?api= 可指向另一台 AstrBot。 */
export function resolveApiBase(search = globalThis.location?.search || "") {
  const override = new URLSearchParams(search).get("api");
  return override ? `${override.replace(/\/+$/, "")}${API_PREFIX}` : API_PREFIX;
}

/** 拼接口 URL，空参数不写进 query。 */
export function apiUrl(endpoint, params, { base = API_PREFIX, origin = globalThis.location?.origin || "http://localhost" } = {}) {
  const path = String(endpoint || "").replace(/^\/+/, "");
  const url = new URL(`${base.replace(/\/+$/, "")}/${path}`, origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return url;
}

/** 页面资源 URL。
 *
 * 宿主下发的模块 URL 带 asset_token（可能在查询串或路径前缀里）。相对资源
 * （卡片样例图 / 角色立绘）不能直接写 ./assets/...，否则会相对根文档解析；
 * 这里以模块 URL 为基准并保留它整段查询串，bootstrap 的模板重定位保持同一规则。
 */
export function assetUrl(relative, moduleHref = import.meta.url) {
  const base = new URL(moduleHref);
  const url = new URL(relative, base);
  if (base.search) url.search = base.search;
  else {
    const token = new URLSearchParams(globalThis.location?.search || "").get("asset_token");
    if (token) url.searchParams.set("asset_token", token);
  }
  url.hash = "";
  return url.toString();
}

function normalizeError(error) {
  if (error instanceof Error) return error;
  if (error && typeof error === "object" && typeof error.message === "string") return new Error(error.message);
  return new Error(String(error || "请求失败"));
}

function saveBlob(blob, filename, documentObject = globalThis.document) {
  if (!documentObject) throw new Error("当前环境无法保存文件");
  const href = URL.createObjectURL(blob);
  const anchor = documentObject.createElement("a");
  anchor.href = href;
  anchor.download = filename || "download";
  documentObject.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(href);
}

/** 创建接口客户端。
 *
 * @param {object} [options]
 * @param {object|null} [options.bridge] 宿主 bridge；undefined 时自动探测
 * @param {Function} [options.fetchImpl]
 * @param {string} [options.search]
 * @param {string} [options.origin]
 * @param {Document} [options.documentObject]
 */
export function createApi(options = {}) {
  const fetchImpl = options.fetchImpl || globalThis.fetch;
  const search = options.search ?? (globalThis.location?.search || "");
  const origin = options.origin || globalThis.location?.origin || "http://localhost";
  const base = resolveApiBase(search);
  const documentObject = options.documentObject || globalThis.document;
  const bridge = options.bridge !== undefined
    ? options.bridge
    : globalThis.window?.AstrBotPluginPage || globalThis.window?.AstrBotPluginView || null;

  async function get(endpoint, params) {
    if (bridge && typeof bridge.apiGet === "function") {
      try {
        return unwrapPayload(await bridge.apiGet(endpoint, params));
      } catch (error) {
        throw normalizeError(error);
      }
    }
    if (typeof fetchImpl !== "function") throw new Error("当前环境没有可用的接口");
    const response = await fetchImpl(apiUrl(endpoint, params, { base, origin }).toString(), {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    return unwrapPayload(await unwrapResponse(response));
  }

  async function post(endpoint, body) {
    if (bridge && typeof bridge.apiPost === "function") {
      try {
        return unwrapPayload(await bridge.apiPost(endpoint, body ?? {}));
      } catch (error) {
        throw normalizeError(error);
      }
    }
    if (typeof fetchImpl !== "function") throw new Error("当前环境没有可用的接口");
    const response = await fetchImpl(apiUrl(endpoint, null, { base, origin }).toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body ?? {}),
    });
    return unwrapPayload(await unwrapResponse(response));
  }

  async function download(endpoint, params, filename) {
    if (bridge && typeof bridge.download === "function") {
      try {
        return await bridge.download(endpoint, params, filename);
      } catch (error) {
        throw normalizeError(error);
      }
    }
    if (typeof fetchImpl !== "function") throw new Error("当前环境没有可用的接口");
    const response = await fetchImpl(apiUrl(endpoint, params, { base, origin }).toString(), {
      credentials: "same-origin",
    });
    if (!response.ok) {
      await unwrapResponse(response);
      throw new Error(`下载失败（HTTP ${response.status}）`);
    }
    saveBlob(await response.blob(), filename, documentObject);
  }

  return { get, post, download, base, hasBridge: Boolean(bridge) };
}

/** 页面默认客户端。studio.js 通过它调用后端。 */
export const api = createApi();
