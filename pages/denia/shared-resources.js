/* Shared resources live outside pages/. Use the authenticated plugin API rather
 * than escaping AstrBot's page asset directory. Both interfaces share this cache.
 */
const resources = new Map();
let loaded = false;
let pending = null;

export function sharedResourceUrl(name) {
  return resources.get(name) || "";
}

export function applySharedResources(root = document) {
  for (const element of root.querySelectorAll("[data-shared-resource]")) {
    const url = sharedResourceUrl(element.dataset.sharedResource);
    if (element.tagName === "LINK") {
      if (url) element.setAttribute("href", url);
    } else {
      if (url) element.setAttribute("src", url);
      element.hidden = !url;
    }
  }
}

export async function loadSharedResources(api, { timeoutMs = 5000 } = {}) {
  if (loaded) return true;
  if (pending) return pending;
  pending = (async () => {
    let timer;
    try {
      const payload = await Promise.race([
        Promise.resolve().then(() => api.get("assets")),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("读取共享图标超时")), timeoutMs); }),
      ]);
      if (!payload?.assets || typeof payload.assets !== "object" || Array.isArray(payload.assets)) {
        throw new Error("共享图标接口没有返回资源列表");
      }
      for (const [name, url] of Object.entries(payload.assets)) {
        if (typeof url === "string" && /^data:image\/(?:png|svg\+xml);base64,[A-Za-z0-9+/=]+$/.test(url)) {
          resources.set(name, url);
        }
      }
      loaded = true;
      return true;
    } catch (error) {
      // Icons must not block configuration, account management or parsing.
      console.warn("[denia] 共享图标未能载入，继续使用文字标识", error);
      return false;
    } finally {
      clearTimeout(timer);
      pending = null;
    }
  })();
  return pending;
}
