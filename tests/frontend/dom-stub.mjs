/**
 * 极简 DOM 桩，供 tests/frontend/ 下的单元测试使用。
 *
 * 为什么自己写而不用 jsdom / happy-dom：
 * - 页面被 AstrBot 塞在**受限 iframe** 里跑，测试只要能覆盖「建节点 → 派发事件
 *   → 读回状态」这条主线，不需要完整 DOM 规范实现；
 * - 零依赖是这个插件 pages/ 目录的一贯约束，测试也不引入 npm 包，
 *   克隆下来 `node --test tests/frontend/` 就能跑。
 *
 * 与早期版本（仓库根目录的 _verify_denia_webui.mjs）相比补齐了：
 * - classList 是真的（原先是空壳 add()，classList.toggle 完全失效，
 *   而 app.js 的 setActiveNav 正是靠它高亮导航）；
 * - 支持 tag / .class / [data-x] / tag[attr="v"] 四类选择器；
 * - document.getElementById、replaceChildren、prepend、matches(":hover")。
 */

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {
      _vars: {},
      setProperty(name, value) {
        this._vars[name] = value;
      },
      removeProperty(name) {
        delete this._vars[name];
      },
    };
    this.attributes = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.id = "";
    this._text = "";
    this._class = new Set();

    const el = this;
    this.classList = {
      add: (...names) => names.forEach((n) => n && el._class.add(n)),
      remove: (...names) => names.forEach((n) => el._class.delete(n)),
      contains: (name) => el._class.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !el._class.has(name) : Boolean(force);
        if (on) el._class.add(name);
        else el._class.delete(name);
        return on;
      },
    };
  }

  get children() {
    return this.childNodes;
  }

  get firstChild() {
    return this.childNodes[0] || null;
  }

  get className() {
    return [...this._class].join(" ");
  }

  set className(value) {
    this._class = new Set(String(value ?? "").split(/\s+/).filter(Boolean));
  }

  set textContent(value) {
    this._text = String(value);
    this.childNodes = [];
  }

  get textContent() {
    return this._text + this.childNodes.map((c) => c.textContent).join("");
  }

  set innerHTML(value) {
    this._text = String(value);
    this.childNodes = [];
  }

  setAttribute(key, value) {
    this.attributes[key] = String(value);
    if (key === "id") {
      this.id = String(value);
      if (globalThis.document?.__byId) globalThis.document.__byId[this.id] = this;
    }
    if (key === "hidden") this.hidden = true;
    if (key === "disabled") this.disabled = true;
    if (key === "value") this.value = String(value);
  }

  getAttribute(key) {
    return key in this.attributes ? this.attributes[key] : null;
  }

  removeAttribute(key) {
    delete this.attributes[key];
  }

  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }

  removeEventListener(type, fn) {
    const list = this.listeners[type] || [];
    const at = list.indexOf(fn);
    if (at >= 0) list.splice(at, 1);
  }

  /** 派发事件；node 可以显式给出 event.target（模拟冒泡来源）。 */
  dispatch(type, extra = {}) {
    const event = {
      type,
      target: extra.target || this,
      currentTarget: this,
      stopPropagation() {},
      preventDefault() {},
      ...extra,
    };
    for (const fn of [...(this.listeners[type] || [])]) fn(event);
    return event;
  }

  appendChild(node) {
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  removeChild(node) {
    this.childNodes = this.childNodes.filter((c) => c !== node);
    node.parentNode = null;
    return node;
  }

  append(...nodes) {
    for (const node of nodes) {
      this.appendChild(node instanceof El ? node : new TextNode(String(node)));
    }
  }

  prepend(...nodes) {
    for (const node of nodes) {
      node.parentNode = this;
      this.childNodes.unshift(node instanceof El ? node : new TextNode(String(node)));
    }
  }

  replaceChildren(...nodes) {
    this.childNodes = [];
    for (const node of nodes) this.appendChild(node);
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  /* ---- 选择器：只支持页面里实际用到的四种形态 ---- */

  _matches(selector) {
    const sel = String(selector).trim();

    // tag[attr="value"]
    let m = /^([a-zA-Z][\w-]*)\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(sel);
    if (m) {
      if (this.tagName !== m[1].toUpperCase()) return false;
      const actual = this.getAttribute(m[2]);
      if (actual === null) return false;
      return m[3] === undefined || actual === m[3];
    }

    // [data-xxx]
    m = /^\[data-([\w-]+)\]$/.exec(sel);
    if (m) {
      const key = m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      return this.dataset[key] !== undefined;
    }

    // .class
    if (sel.startsWith(".")) return this._class.has(sel.slice(1));

    // tag
    if (/^[a-zA-Z][\w-]*$/.test(sel)) return this.tagName === sel.toUpperCase();

    return false;
  }

  _walk(out, selector) {
    for (const child of this.childNodes) {
      if (child._matches(selector)) out.push(child);
      child._walk(out, selector);
    }
    return out;
  }

  querySelectorAll(selector) {
    return this._walk([], selector);
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  /** 按谓词深度查找（选择器表达不了的断言用它）。 */
  _find(pred, out = []) {
    for (const child of this.childNodes) {
      if (pred(child)) out.push(child);
      child._find(pred, out);
    }
    return out;
  }

  /** 只支持 :hover / :focus —— banner.js 轮换时会问。 */
  matches(selector) {
    return selector === ":hover" ? Boolean(this._hover) : false;
  }
}

class TextNode {
  constructor(text) {
    this._text = String(text);
    this.parentNode = null;
    this.childNodes = [];
  }
  get textContent() {
    return this._text;
  }
  set textContent(value) {
    this._text = String(value);
  }
  _matches() {
    return false;
  }
  _walk() {}
  _find() {}
  appendChild(node) {
    this.childNodes.push(node);
    return node;
  }
  removeChild(node) {
    this.childNodes = this.childNodes.filter((c) => c !== node);
    return node;
  }
  remove() {}
}

TextNode.prototype.parentNode = null;

/**
 * 装一套全局 DOM 环境。返回 teardown 便于测试间复位。
 * @param {object} [options]
 * @param {Record<string, string>} [options.ids] 需要预置的元素 id
 * @param {string} [options.search] location.search
 * @param {string} [options.hash] location.hash
 */
export function installDom({ ids = {}, search = "", hash = "", origin = "http://localhost" } = {}) {
  const byId = {};
  const documentElement = new El("html");
  const body = new El("body");

  const document = {
    documentElement,
    body,
    createElement: (tag) => new El(tag),
    createTextNode: (text) => new TextNode(text),
    createDocumentFragment: () => new El("#fragment"),
    getElementById: (id) => byId[id] || null,
    querySelector: (sel) => documentElement._walk([], sel)[0] || null,
    querySelectorAll: (sel) => documentElement._walk([], sel),
    addEventListener() {},
    hidden: false,
    __byId: byId,
  };

  for (const [id, tag] of Object.entries(ids)) {
    const el = new El(tag);
    el.id = id;
    byId[id] = el;
    if (tag !== "html" && tag !== "body") documentElement.appendChild(el);
  }
  documentElement.appendChild(body);

  const location = { hash, search, origin, href: `${origin}/${search}` };
  const window = {
    document,
    location,
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    addEventListener() {},
    removeEventListener() {},
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
    setInterval: () => 0,
    clearInterval: (id) => clearInterval(id),
  };

  const previous = {
    document: globalThis.document,
    window: globalThis.window,
    location: globalThis.location,
    Node: globalThis.Node,
    Element: globalThis.Element,
  };

  globalThis.document = document;
  globalThis.window = window;
  globalThis.location = location;
  globalThis.Node = El;
  globalThis.Element = El;

  return {
    document,
    window,
    location,
    El,
    teardown() {
      globalThis.document = previous.document;
      globalThis.window = previous.window;
      globalThis.location = previous.location;
      globalThis.Node = previous.Node;
      globalThis.Element = previous.Element;
    },
  };
}

export { El, TextNode };
