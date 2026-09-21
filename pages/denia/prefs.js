/* 达妮娅分享 · 界面外观偏好（从 app.js 抽出，app.js 只留视图框架）
 *
 * 职责：主题色 / 圆角 / 紧凑 / 动效 / 主题覆盖 的存取与 CSS 变量派生。
 * - 存：POST appearance → 插件数据目录 webui_appearance.json（只影响本页面）
 * - 取：loadPrefs(api) 在启动时读回；api 由调用方传入，避免 prefs ↔ app 循环依赖
 * - 派生：applyPrefs() 把偏好翻译成 <html> 上的 data-* 与 CSS 变量
 */

export const ACCENT_PRESETS = {
  "": null, // 跟随默认
  "#2F6FDD": { a: "#2F6FDD", b: "#7A5CFF", dark: ["#6EA6F5", "#9A7CFF"] },
  "#FB7299": { a: "#FB7299", b: "#FF9A6C", dark: ["#FB8AB0", "#FF9A6C"] },
  "#2EC4B6": { a: "#2EC4B6", b: "#5C8AFF", dark: ["#4FD8CB", "#7FA3FF"] },
  "#F59E0B": { a: "#F59E0B", b: "#EF6351", dark: ["#F7B84B", "#F2806E"] },
  "#8B5CF6": { a: "#8B5CF6", b: "#EC4899", dark: ["#A78BFA", "#F472B6"] },
  "#10B981": { a: "#10B981", b: "#3B82F6", dark: ["#34D399", "#60A5FA"] },
};

export const prefs = {
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

export function applyPrefs() {
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

export async function loadPrefs(api) {
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
