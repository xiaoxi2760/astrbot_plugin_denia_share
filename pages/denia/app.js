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

  ctx.setConnection(true, "已连接");
  await switchView("overview");
}

boot();
