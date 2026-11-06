/* 通用 UI 小工具：DOM 构造、提示条、确认框、格式化。
 * 全部零依赖，避免在受限 iframe 里受 CSP 与网络影响。
 */

/** 建元素。props 里的函数值会当作事件监听，其余当属性；style 支持对象。 */
export function h(tag, props = null, children = null) {
  const node = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value;
      else if (key === "style" && typeof value === "object") {
        Object.assign(node.style, value);
      } else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === "dataset" && typeof value === "object") {
        Object.assign(node.dataset, value);
      } else if (value === true) {
        node.setAttribute(key, "");
      } else {
        node.setAttribute(key, String(value));
      }
    }
  }
  append(node, children);
  return node;
}

/** 追加子节点，自动跳过 null / false，字符串转文本节点。 */
export function append(parent, children) {
  if (children === null || children === undefined || children === false) return parent;
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    parent.appendChild(
      child instanceof Node ? child : document.createTextNode(String(child)),
    );
  }
  return parent;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/**
 * 卡片块：标题 + 说明 + 内容。
 *
 * tone 为 "warn" / "err" 时给整张卡加一道状态色边框与角标 —— 用于「这张卡里
 * 有需要用户处理的事」，例如缓存目录回退。只写在正文小字里容易被划过去，
 * 而这类状态往往决定了视频发不发得出去。
 */
export function card(title, subtitle, children, actions = null, tone = "") {
  const head = h("div", { class: "card-head" }, [
    h("h2", { text: title || "" }),
    tone ? h("span", { class: `pill ${tone}`, text: tone === "err" ? "异常" : "需处理" }) : null,
    subtitle ? h("span", { class: "sub", text: subtitle }) : null,
    actions || null,
  ]);
  return h(
    "section",
    { class: `card${tone ? ` card-${tone}` : ""}` },
    [head, append(h("div"), children)],
  );
}

export function statCard(label, value, meta) {
  return h("div", { class: "stat" }, [
    h("span", { class: "k", text: label }),
    h("span", { class: "v", text: value }),
    meta ? h("span", { class: "m", text: meta }) : null,
  ]);
}

export function pill(text, kind = "") {
  return h("span", { class: `pill ${kind}`.trim(), text });
}

export function emptyBox(text) {
  return h("div", { class: "empty", text });
}

export function placeholder(text) {
  return h("div", { class: "shot-placeholder", text });
}

export function spinner() {
  return h("span", { class: "spinner" });
}

/* ---------------- 格式化 ---------------- */

export function fmtTime(seconds) {
  if (!seconds) return "—";
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

export function fmtBytes(size) {
  const value = Number(size) || 0;
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let scaled = value / 1024;
  let index = 0;
  while (scaled >= 1024 && index < units.length - 1) {
    scaled /= 1024;
    index += 1;
  }
  return `${scaled.toFixed(1)} ${units[index]}`;
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined) return "—";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`;
}

export const VIA_LABELS = {
  auto: "链接自动",
  command: "命令",
  webui: "网页手动",
};

/* ---------------- 提示条 ---------------- */

const TOAST_MS = 3600;

export function toast(message, kind = "") {
  const host = document.getElementById("toast-host");
  if (!host) return;
  const node = h("div", { class: `toast ${kind}`.trim(), text: message });
  host.appendChild(node);
  setTimeout(() => node.remove(), TOAST_MS);
}

/* ---------------- 确认框 ---------------- */

export function confirmDialog({ title, body, confirmText = "确认", danger = false }) {
  return new Promise((resolve) => {
    const host = document.getElementById("modal-host");
    const close = (result) => {
      host.hidden = true;
      clear(host);
      resolve(result);
    };
    const modal = h("div", { class: "modal" }, [
      h("h2", { text: title }),
      h("p", { class: "card-note", text: body }),
      h("div", { class: "modal-foot" }, [
        h("button", { class: "btn", type: "button", onClick: () => close(false) }, "取消"),
        h(
          "button",
          {
            class: `btn ${danger ? "danger" : "primary"}`.trim(),
            type: "button",
            onClick: () => close(true),
          },
          confirmText,
        ),
      ]),
    ]);
    clear(host);
    host.appendChild(modal);
    host.hidden = false;
    host.onclick = (event) => {
      if (event.target === host) close(false);
    };
  });
}

/* ---------------- 通用弹窗 ---------------- */

/**
 * 打开一个自定义弹窗。
 * @param {object} options
 * @param {string} options.title 标题
 * @param {Node|Node[]} options.body 内容
 * @param {string} [options.width] 最大宽度，如 "720px"
 * @param {(result:any)=>void} [options.onClose] 关闭回调
 * @returns {{close: (result:any)=>void, node: HTMLElement, host: HTMLElement}}
 */
export function openModal({ title, body, width = "640px", onClose = null } = {}) {
  const host = document.getElementById("modal-host");
  const modal = h("div", { class: "modal", style: { maxWidth: width } }, [
    h("h2", { text: title || "" }),
    append(h("div", { class: "modal-body" }), body),
    h("div", { class: "modal-foot" }, [
      h("button", { class: "btn", type: "button", onClick: () => close("close") }, "关闭"),
    ]),
  ]);

  const close = (result) => {
    host.hidden = true;
    host.onclick = null;
    clear(host);
    if (typeof onClose === "function") onClose(result);
  };

  clear(host);
  host.appendChild(modal);
  host.hidden = false;
  host.onclick = (event) => {
    if (event.target === host) close("backdrop");
  };

  return { close, node: modal, host };
}

/* ---------------- 忙碌态 ---------------- */

export function withBusy(button, busy, busyText = "处理中…") {
  if (!button) return () => {};
  if (busy) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = busyText;
    return () => {
      button.disabled = false;
      button.textContent = original;
    };
  }
  button.disabled = false;
  return () => {};
}

/* ---------------- 表单片段 ---------------- */

export function switchControl(checked, onChange, onText = "开启", offText = "关闭") {
  // 文案节点留引用，切换时就地更新 —— 不能指望调用方重建 DOM：
  // 配置页现在不重建（重建会让正在输入的文本框失焦），不就地改就会出现
  // 「开关拨到了「关闭」，旁边仍写「开启」」。
  const text = h("span", { class: "switch-text", text: checked ? onText : offText });
  const input = h("input", {
    type: "checkbox",
    checked: checked ? true : null,
    onChange: (event) => {
      const next = event.target.checked;
      text.textContent = next ? onText : offText;
      onChange(next);
    },
  });
  return h("label", { class: "switch" }, [
    input,
    h("span", { class: "track" }),
    text,
  ]);
}

export function secretInput(value, onInput, placeholder = "") {
  let visible = false;
  const input = h("input", {
    class: "input",
    type: "password",
    value: value || "",
    placeholder,
    spellcheck: "false",
    onInput: (event) => onInput(event.target.value),
  });
  const toggle = h(
    "button",
    {
      class: "secret-toggle",
      type: "button",
      title: "显示 / 隐藏",
      onClick: () => {
        visible = !visible;
        input.type = visible ? "text" : "password";
        toggle.textContent = visible ? "隐藏" : "显示";
      },
    },
    "显示",
  );
  return h("div", { class: "secret-wrap" }, [input, toggle]);
}

export function previewFrame(dataUrl, fallback = "暂无预览") {
  const frame = h("div", { class: "shot-frame" });
  if (dataUrl) {
    frame.appendChild(h("img", { src: dataUrl, alt: "预览" }));
  } else {
    frame.appendChild(placeholder(fallback));
  }
  return frame;
}
