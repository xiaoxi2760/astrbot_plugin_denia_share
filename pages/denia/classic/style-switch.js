/* 界面切换决策：保存 WEBUI_STYLE 之后，判断要不要换界面、换成哪个地址。
 *
 * 经典界面和自设主题复用 createConfigView 的保存逻辑。
 *
 * 只做决策，不碰业务：真正跳转由调用方执行（applyStyleSwitch）。
 */

/** 决定界面形态的配置键。 */
export const STYLE_KEY = "WEBUI_STYLE";
export const STYLE_CLASSIC = "classic";

/** 界面 ID 的格式与主题清单一致，是否已安装由插件确认。 */
export function normalizeStyleValue(raw) {
  if (typeof raw !== "string") return null;
  const value = raw.trim().toLowerCase();
  return /^[a-z][a-z0-9_-]{0,31}$/.test(value) ? value : null;
}

/** ?ui=classic 临时入口；只认 classic，其他 ui 值一律忽略。 */
export function styleOverrideFromSearch(search = "") {
  const value = new URLSearchParams(search).get("ui");
  return value && value.trim().toLowerCase() === STYLE_CLASSIC ? STYLE_CLASSIC : null;
}

/** 生效的界面：临时入口优先，其次已保存值；非法/缺省按经典界面算。 */
export function effectiveStyle(savedValue, override = null) {
  if (override) return override;
  return normalizeStyleValue(savedValue) || STYLE_CLASSIC;
}

/* 两个界面各自的设置页 hash 名字不同（经典 #/config，泡泡工坊 #/settings）。
   换界面时把设置页映射到对应的那一个，其余 hash 原样保留 —— 目标界面
   认不出来的 hash 会自己回退到默认页，不用在这里重写路由。 */
export function mapHashForStyle(hash, style) {
  const raw = String(hash || "").replace(/^#\/?/, "").trim();
  if (!raw) return "";
  const view = raw.split("?")[0];
  if (view !== "config" && view !== "settings") return hash || "";
  return `#/${style === STYLE_CLASSIC ? "config" : "settings"}`;
}

/**
 * 换界面要用的地址：去掉临时的 ui 强制值（否则新界面会被拉回旧界面），
 * 其余查询参数原样保留（宿主鉴权 token 等不能丢）。
 */
export function styleSwitchUrl({ href = "", hash = "", style = STYLE_CLASSIC } = {}) {
  let url;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  url.searchParams.delete("ui");
  const nextHash = mapHashForStyle(hash || url.hash, style);
  url.hash = nextHash;
  return url.toString();
}

/**
 * 保存之后要不要换界面。
 *
 * 换界面的条件只有一条：这次保存真的把「生效界面」改了。
 *  - 没保存 WEBUI_STYLE（只是改了别的配置）→ 不换；
 *  - 保存后生效界面和之前一样（选了同一个值）→ 不换；
 *  - 保存失败 / 部分失败 → 由调用方先判掉，不进这里。
 */
export function planStyleSwitch({
  savedKeys = [],
  previousStyle = null,
  nextStyle = null,
  search = "",
  href = "",
  hash = "",
  override = undefined,
} = {}) {
  const keys = Array.isArray(savedKeys) ? savedKeys : [];
  if (!keys.includes(STYLE_KEY)) {
    return { navigate: false, reason: "style-not-saved", url: null };
  }
  const active = override === undefined ? styleOverrideFromSearch(search) : override;
  const before = effectiveStyle(previousStyle, active);
  const after = effectiveStyle(nextStyle, null);
  if (before === after) {
    return { navigate: false, reason: "style-unchanged", url: null };
  }
  const url = styleSwitchUrl({ href, hash, style: after });
  if (!url) {
    return { navigate: false, reason: "no-url", url: null };
  }
  return { navigate: true, reason: "style-changed", url, style: after, previousStyle: before };
}

/**
 * 执行跳转。默认在当前 iframe 内 replace（不跳宿主页，不重启插件进程）。
 * navigate 可注入，给测试用。
 */
export function applyStyleSwitch(plan, { locationObject = globalThis.location, navigate } = {}) {
  if (!plan || !plan.navigate || !plan.url) return false;
  if (typeof navigate === "function") {
    navigate(plan.url);
    return true;
  }
  if (!locationObject) return false;
  if (typeof locationObject.replace === "function") {
    // replace() with only a new hash is an in-page navigation: bootstrap would
    // never run again. Explicitly reload that case to actually swap the UI.
    let hashOnly = false;
    try {
      const current = new URL(locationObject.href);
      const next = new URL(plan.url);
      hashOnly = current.origin === next.origin
        && current.pathname === next.pathname && current.search === next.search;
    } catch { /* A non-browser test double may have no usable href. */ }
    locationObject.replace(plan.url);
    if (hashOnly && typeof locationObject.reload === "function") locationObject.reload();
    return true;
  }
  locationObject.href = plan.url;
  return true;
}
