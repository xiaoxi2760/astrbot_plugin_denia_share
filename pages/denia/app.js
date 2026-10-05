/* 希望解析器 · 插件页面入口
 *
 * 职责：等 bridge 就绪 → 渲染左导航 → 挂载当前视图 → 提供共用的 API 封装。
 * 业务都在 views/ 下，界面外观偏好在 prefs.js，本文件保持「框架层」的轻量。
 */

import { h, clear, toast } from "./ui.js";
import { ACCENT_PRESETS, prefs, applyPrefs, loadPrefs } from "./prefs.js";
import { initBanner } from "./banner.js";
import { createOverviewView } from "./views/overview.js";
import { createParseView } from "./views/parse.js";
import { createCacheView } from "./views/cache.js";
import { createConfigView } from "./views/config.js";
import { createAppearanceView } from "./views/appearance.js";

const bridge = window.AstrBotPluginPage;
const hasBridge = Boolean(bridge && typeof bridge.apiGet === "function");

/* 开发态直连后端：bridge 不存在时（本地用静态服务器打开 index.html）改走 fetch。
 * 两条路径必须给出**完全一样**的结果，否则同一段视图代码在开发和生产下行为分叉 ——
 * 这正是本地改完、上线才发现问题的那类坑。
 *   · 默认打 /api/plug/<插件名>/<路径>，对应 AstrBot 自身的插件 API 前缀
 *   · 可用 ?api=http://host:port 覆盖，指向一个跑着本插件的 AstrBot 实例
 *     （例：http://localhost:6180/?api=http://127.0.0.1:6180）
 */
const API_BASE = (() => {
  const override = new URLSearchParams(location.search).get("api");
  if (override) return override.replace(/\/+$/, "") + "/api/plug/astrbot_plugin_denia_share";
  return "/api/plug/astrbot_plugin_denia_share";
})();

/** 视图清单。测试用它校验左导航（index.html 的 data-view）与路由不会脱节。 */
export const VIEWS = {
  overview: {
    title: "总览",
    sub: "运行状态、平台开关、最近解析",
    factory: createOverviewView,
  },
  parse: {
    title: "解析",
    sub: "手动解析与截图测试，管理自定义解析器",
    factory: createParseView,
  },
  cache: {
    title: "缓存",
    sub: "解析记录与缓存文件的管理台",
    factory: createCacheView,
  },
  appearance: {
    title: "外观",
    sub: "界面主题与分享卡片样式设计，实时预览",
    factory: createAppearanceView,
  },
  config: {
    title: "配置",
    sub: "按「大类 → 分组 → 子组」组织的全部配置项，保存后立即生效",
    factory: createConfigView,
  },
};

/** 从 HTTP 响应里取出业务对象，失败形态统一转成异常。
 *
 * 后端成功时返回的是**裸业务对象**（不是 {status, message, data} 信封），
 * 失败时返回 {status: "error", message, data:{}} —— bridge 正是据此抛错的。
 * 这里保持同样的判据，开发态和生产态才能给视图一样的数据。
 */
export async function unwrapResponse(resp) {
  let body = null;
  try {
    body = await resp.json();
  } catch {
    body = null;
  }
  const message = (body && body.message) || "";
  if (body && body.status === "error") {
    throw new Error(message || `请求失败（HTTP ${resp.status}）`);
  }
  // 非 2xx 且没有错误信封：不能把 HTML 错误页或空体当业务数据交给视图
  if (!resp.ok) {
    throw new Error(message || `请求失败（HTTP ${resp.status}）`);
  }
  return body;
}

async function devGet(endpoint, params) {
  const url = new URL(`${API_BASE}/${endpoint}`, location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return unwrapResponse(await fetch(url.toString(), { headers: { Accept: "application/json" } }));
}

async function devPost(endpoint, body) {
  const resp = await fetch(`${API_BASE}/${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  return unwrapResponse(resp);
}

async function devDownload(endpoint, params, filename) {
  const url = new URL(`${API_BASE}/${endpoint}`, location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }
  const resp = await fetch(url.toString());
  if (!resp.ok) {
    await unwrapResponse(resp); // 复用错误判据，抛出的异常信息一致
    throw new Error(`下载失败（HTTP ${resp.status}）`);
  }
  const href = URL.createObjectURL(await resp.blob());
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = filename || "download";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(href);
}

/** 统一的接口调用封装：有 bridge 走 bridge，没有就直连后端（本地开发）。 */
export const api = {
  get(endpoint, params) {
    return hasBridge ? bridge.apiGet(endpoint, params) : devGet(endpoint, params);
  },
  post(endpoint, body) {
    return hasBridge ? bridge.apiPost(endpoint, body) : devPost(endpoint, body);
  },
  download(endpoint, params, filename) {
    return hasBridge
      ? bridge.download(endpoint, params, filename)
      : devDownload(endpoint, params, filename);
  },
};

/* 界面外观偏好已抽到 ./prefs.js（ACCENT_PRESETS / prefs / applyPrefs / loadPrefs），
 * 这里只保留视图框架。 */

const state = {
  view: "overview",
  instance: null,
  title: document.getElementById("view-title"),
  sub: document.getElementById("view-sub"),
  body: document.getElementById("stage-body"),
  pill: document.getElementById("head-pill"),
  dot: document.getElementById("rail-dot"),
  status: document.getElementById("rail-status"),
  badge: document.getElementById("cache-badge"),
  version: document.getElementById("brand-version"),
  reloadButton: document.getElementById("btn-reload"),
};

/** 提供给视图的控制台上下文。 */
const ctx = {
  api,
  prefs,
  accentPresets: ACCENT_PRESETS,
  applyPrefs,
  async savePrefs(patch) {
    Object.assign(prefs, patch);
    applyPrefs();
    try {
      await api.post("appearance", { prefs: patch });
    } catch (error) {
      toast(`外观偏好保存失败：${error.message || error}`, "err");
    }
  },
  setHead(text, kind = "") {
    if (!text) {
      state.pill.hidden = true;
      state.pill.textContent = "";
      state.pill.className = "pill";
      return;
    }
    state.pill.hidden = false;
    state.pill.textContent = text;
    state.pill.className = `pill ${kind}`.trim();
  },
  setBadge(count) {
    const value = Number(count) || 0;
    state.badge.hidden = value <= 0;
    state.badge.textContent = value > 999 ? "999+" : String(value);
  },
  setVersion(text) {
    if (text) state.version.textContent = text;
  },
  setConnection(ok, text) {
    state.dot.className = `dot ${ok ? "is-ok" : "is-err"}`;
    state.status.textContent = text;
  },
  goTo(view) {
    switchView(view);
  },
};

function setActiveNav(view) {
  document.querySelectorAll(".rail-item").forEach((item) => {
    item.classList.toggle("is-active", item.dataset.view === view);
  });
}

/* ---------------- 视图 ↔ URL hash 同步 ----------------
 *
 * 之前 state.view 只是个普通变量：刷新永远回总览、浏览器后退键无效、
 * 「缓存」页没法发链接给别人。接上 hash 后这三件事都自然成立。
 *
 * 格式统一为 #/<view>（多一层斜杠是为了和 SPA 习惯一致，也给以后
 * #/cache/123 这类带参路由留位置）。
 */

export function viewFromHash() {
  const raw = location.hash.replace(/^#\/?/, "").trim();
  return raw && VIEWS[raw] ? raw : null;
}

export function writeHash(view) {
  const next = `#/${view}`;
  // 相同 hash 不重复赋值：重复触发 hashchange 会白白重挂一次视图
  if (location.hash !== next) location.hash = next;
}

/** hash 变化（用户按后退/前进，或直接改地址）时切视图。 */
function onHashChange() {
  const view = viewFromHash();
  // 与当前一致说明是 switchView 自己写进去的，不必重挂
  if (!view || view === state.view) return;
  switchView(view, { updateHash: false });
}

async function switchView(view, options = {}) {
  const config = VIEWS[view];
  if (!config) return;
  const { updateHash = true } = options;

  if (state.instance && typeof state.instance.unmount === "function") {
    try {
      state.instance.unmount();
    } catch (error) {
      console.error("视图卸载失败", error);
    }
  }
  state.instance = null;

  state.view = view;
  state.title.textContent = config.title;
  state.sub.textContent = config.sub;
  setActiveNav(view);
  ctx.setHead("");
  clear(state.body);
  if (updateHash) writeHash(view);

  const holder = h("div", { class: "view" });
  state.body.appendChild(holder);

  try {
    const instance = config.factory(ctx);
    state.instance = instance;
    await instance.mount(holder);
  } catch (error) {
    console.error("视图加载失败", error);
    clear(holder);
    holder.appendChild(
      h("div", { class: "empty", text: `页面加载失败：${error.message || error}` }),
    );
    toast(`加载失败：${error.message || error}`, "err");
  }
}

function bindNav() {
  document.getElementById("rail-nav").addEventListener("click", (event) => {
    const button = event.target.closest(".rail-item");
    if (!button) return;
    if (button.dataset.view === state.view) return;
    switchView(button.dataset.view);
  });

  // 后退/前进键与直接改地址栏都走这里
  window.addEventListener("hashchange", onHashChange);

  state.reloadButton.addEventListener("click", async () => {
    state.reloadButton.disabled = true;
    state.reloadButton.textContent = "刷新中…";
    try {
      if (state.instance && typeof state.instance.refresh === "function") {
        await state.instance.refresh();
      } else {
        await switchView(state.view);
      }
    } finally {
      state.reloadButton.disabled = false;
      state.reloadButton.textContent = "刷新";
    }
  });
}

async function boot() {
  bindNav();
  // 页头彩蛋文案在静态区、不依赖 bridge，第一时间挂上
  initBanner();

  // 没有 bridge 不再是死路：api.* 会自动改走 fetch 直连后端（见 API_BASE），
  // 这样本地用静态服务器打开 index.html 也能真调通接口。
  // 提示只挂在左下角常驻状态位 —— 往 stage-body 里插提示条会被紧接着的
  // switchView → clear(state.body) 抹掉，等于白写。
  const devMode = !hasBridge;
  if (devMode) {
    ctx.setConnection(true, `开发模式 · ${API_BASE}`);
  } else {
    try {
      await bridge.ready();
    } catch (error) {
      console.error("等待 bridge 上下文失败", error);
    }

    // Dashboard 主题切换时重算偏好派生色；theme_override 为 follow 时直接跟随
    if (typeof bridge.onContext === "function") {
      bridge.onContext(() => {
        if (prefs.theme_override !== "follow") {
          document.documentElement.dataset.theme = prefs.theme_override;
        }
        applyPrefs();
      });
    }
  }

  await loadPrefs(api);

  if (!devMode) ctx.setConnection(true, "已连接");
  // 尊重地址栏里的 hash：支持深链与刷新后停在原页面
  await switchView(viewFromHash() || "overview");
}

/* 正常入口路径下直接启动。
 * 置了 __DENIA_NO_AUTOBOOT__ 则只加载模块、不挂视图 —— tests/frontend/ 靠这个
 * 单独 import 本文件来测 unwrapResponse / viewFromHash 等纯函数：那些函数住在
 * app.js 里是因为它们只服务于这一处流程，拆出去反而多一层间接。
 */
if (!globalThis.__DENIA_NO_AUTOBOOT__) {
  boot();
}
