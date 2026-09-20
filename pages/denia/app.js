/* 达妮娅分享 · 插件页面入口
 *
 * 职责：等 bridge 就绪 → 渲染左导航 → 挂载当前视图 → 提供共用的 API 封装。
 * 业务都在 views/ 下，本文件保持「框架层」的轻量。
 */

import { h, clear, toast } from "./ui.js";
import { createOverviewView } from "./views/overview.js";
import { createParseView } from "./views/parse.js";
import { createCacheView } from "./views/cache.js";
import { createConfigView } from "./views/config.js";
import { createAppearanceView } from "./views/appearance.js";

const bridge = window.AstrBotPluginPage;

const VIEWS = {
  overview: {
    title: "总览",
    sub: "运行状态、平台开关、最近解析",
    factory: createOverviewView,
  },
  parse: {
    title: "解析",
    sub: "手动输入链接，立刻看到卡片效果",
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
    sub: "全部配置项，保存后立即生效",
    factory: createConfigView,
  },
};

/** 统一的接口调用封装：后端返回普通 JSON 时 bridge 直接给出业务对象。 */
export const api = {
  async get(endpoint, params) {
    return bridge.apiGet(endpoint, params);
  },
  async post(endpoint, body) {
    return bridge.apiPost(endpoint, body);
  },
  download(endpoint, params, filename) {
    return bridge.download(endpoint, params, filename);
  },
};

/* ---------------- 界面外观偏好 ---------------- */

const ACCENT_PRESETS = {
  "": null, // 跟随默认
  "#2F6FDD": { a: "#2F6FDD", b: "#7A5CFF", dark: ["#6EA6F5", "#9A7CFF"] },
  "#FB7299": { a: "#FB7299", b: "#FF9A6C", dark: ["#FB8AB0", "#FF9A6C"] },
  "#2EC4B6": { a: "#2EC4B6", b: "#5C8AFF", dark: ["#4FD8CB", "#7FA3FF"] },
  "#F59E0B": { a: "#F59E0B", b: "#EF6351", dark: ["#F7B84B", "#F2806E"] },
  "#8B5CF6": { a: "#8B5CF6", b: "#EC4899", dark: ["#A78BFA", "#F472B6"] },
  "#10B981": { a: "#10B981", b: "#3B82F6", dark: ["#34D399", "#60A5FA"] },
};

const prefs = {
  accent: "",
  accent_custom: "",
  radius: "m",
  compact: false,
  animations: true,
  theme_override: "follow",
};

function isDark() {
  return document.documentElement.dataset.theme === "dark";
}

function hexToRgb(hex) {
  const v = hex.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16));
}

function applyPrefs() {
  const root = document.documentElement;
  root.dataset.radius = prefs.radius || "m";
  root.dataset.compact = prefs.compact ? "1" : "0";
  root.dataset.motion = prefs.animations ? "1" : "0";

  const key = prefs.accent || "";
  const custom = prefs.accent_custom || "";
  const src = key === "custom" ? custom : key;
  const preset = ACCENT_PRESETS[key];
  let a;
  let b;
  if (preset) {
    [a, b] = isDark() ? preset.dark : [preset.a, preset.b];
  } else if (src && /^#[0-9a-fA-F]{6}$/.test(src)) {
    a = src;
    b = src;
  }
  if (a) {
    const [r, g, bl] = hexToRgb(a);
    const dark = isDark();
    root.style.setProperty("--accent", a);
    root.style.setProperty("--accent-2", b);
    root.style.setProperty("--accent-soft", `rgba(${r},${g},${bl},${dark ? 0.18 : 0.13})`);
    root.style.setProperty("--accent-ring", `rgba(${r},${g},${bl},0.4)`);
    root.style.setProperty("--accent-text", dark ? "#0d1420" : "#ffffff");
    root.style.setProperty("--bg-grad-a", `rgba(${r},${g},${bl},${dark ? 0.09 : 0.07})`);
  } else {
    for (const name of ["--accent", "--accent-2", "--accent-soft", "--accent-ring", "--accent-text", "--bg-grad-a"]) {
      root.style.removeProperty(name);
    }
  }
}

async function loadPrefs() {
  try {
    const payload = await api.get("appearance");
    const saved = (payload && payload.prefs) || {};
    for (const key of Object.keys(prefs)) {
      if (saved[key] !== undefined && saved[key] !== null) prefs[key] = saved[key];
    }
  } catch (error) {
    console.warn("读取外观偏好失败，使用默认值", error);
  }
  // 主题覆盖：follow 时不碰 data-theme（跟随 Dashboard）
  if (prefs.theme_override === "light" || prefs.theme_override === "dark") {
    document.documentElement.dataset.theme = prefs.theme_override;
  }
  applyPrefs();
}

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

async function switchView(view) {
  const config = VIEWS[view];
  if (!config) return;

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

  if (!bridge || typeof bridge.ready !== "function") {
    ctx.setConnection(false, "bridge 未就绪");
    state.body.appendChild(
      h("div", {
        class: "empty",
        text:
          "没有拿到 AstrBotPluginPage bridge。请确认插件已启用，并从插件详情页打开本页面。",
      }),
    );
    return;
  }

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

  await loadPrefs();

  ctx.setConnection(true, "已连接");
  await switchView("overview");
}

boot();
