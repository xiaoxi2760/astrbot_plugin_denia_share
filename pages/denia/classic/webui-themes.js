/* Optional interfaces are installed explicitly; loading this panel never downloads one. */
import { h, card, toast, withBusy } from "./ui.js";

const MAX_ARCHIVE = 20 * 1024 * 1024;

export function createWebUIThemesPanel(ctx, { onInstalled } = {}) {
  let supported = false;
  let installed = [];
  let busy = false;
  const status = h("p", { class: "muted", text: "经典界面随插件提供，自设界面需要单独安装。" });
  const url = h("input", { class: "input", type: "url", placeholder: "主题 ZIP 的 HTTPS 下载地址", autocomplete: "off" });
  const digest = h("input", { class: "input", type: "text", placeholder: "SHA-256（可选）", autocomplete: "off", spellcheck: "false" });
  const file = h("input", { type: "file", accept: ".zip,application/zip", "aria-label": "选择自设 WebUI 安装包" });
  const node = card("自设 WebUI", "按需安装，不随插件默认打包", [
    status,
    h("p", { class: "muted", text: "安装后，在下方「WebUI 样式」中选择并保存。未安装或载入失败时自动使用经典界面。" }),
    h("div", { class: "field-row" }, [file, h("button", { class: "btn", type: "button", onClick: (event) => importFile(event.currentTarget) }, "导入安装包")]),
    h("div", { class: "field-row" }, [url, digest, h("button", { class: "btn", type: "button", onClick: (event) => download(event.currentTarget) }, "下载并安装")]),
    h("a", { href: "https://github.com/xiaoxi2760/denia-share-themes/releases", target: "_blank", rel: "noopener noreferrer", text: "前往主题库下载 ZIP 安装包" }),
  ]);

  function refresh() {
    status.textContent = installed.length
      ? `已安装：${installed.map((item) => `${item.name} ${item.version}`).join("、")}`
      : "尚未安装自设界面。经典界面可直接使用。";
  }
  async function load() {
    try {
      const result = await ctx.api.get("webui/themes");
      if (!Array.isArray(result?.installed)) throw new Error("自设界面接口不可用");
      supported = true;
      installed = result.installed;
      refresh();
    } catch { supported = false; }
    return supported;
  }
  async function install(body, button) {
    if (busy) return;
    busy = true;
    const restore = withBusy(button, true, "安装中…");
    try {
      const result = await ctx.api.post("webui/themes/install", body);
      await load();
      onInstalled?.();
      toast(result?.message || "主题已安装，请选择对应 WebUI 样式并保存", result?.refresh_failed ? "warn" : "ok");
    } catch (error) { toast(error?.message || "安装失败，现有界面保留", "err"); }
    finally { busy = false; restore(); }
  }
  async function importFile(button) {
    const archive = file.files?.[0];
    if (!archive) return toast("请先选择 ZIP 安装包", "warn");
    if (archive.size > MAX_ARCHIVE) return toast("安装包不能超过 20 MB", "warn");
    try {
      const encoded = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
        reader.onerror = () => reject(new Error("读取安装包失败"));
        reader.readAsDataURL(archive);
      });
      await install({ archive_base64: encoded, sha256: digest.value.trim() }, button);
    } catch (error) { toast(error?.message || "读取安装包失败", "err"); }
  }
  async function download(button) {
    if (!url.value.trim()) return toast("请填写主题包的 HTTPS 下载地址", "warn");
    await install({ url: url.value.trim(), sha256: digest.value.trim() }, button);
  }
  return { node, load, get supported() { return supported; }, get installed() { return installed; } };
}
