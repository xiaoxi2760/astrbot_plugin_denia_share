# 页面测试

`pages/denia/` 的离线单元测试。**零依赖**：只用 Node 内置的 `node:test` 与
`node:assert`，不引入 jsdom / 测试框架 —— 页面本来就要在 AstrBot 的受限 iframe
里跑、不许有外部网络请求，测试沿用同一条约束，装完 Node 就能跑。

## 跑法

在插件根目录下：

```bash
# 页面测试（Node 22 的 --test 收目录会当成模块去加载，必须给 glob）
node --test "tests/frontend/*.test.mjs"

# 或者用附带的脚本，一次跑完前后端
py -3 tests/run_tests.py
```

单文件：

```bash
node --test tests/frontend/routing.test.mjs
```

Python 侧是 `tests/test_web_compat.py`（stdlib unittest）：

```bash
py -3 -m unittest discover -s tests -t .
```

## 文件

| 文件 | 守的是什么 |
|---|---|
| `dom-stub.mjs` | 共享 DOM 桩。不是被测对象，只提供「建节点 / 派发事件 / 读状态」 |
| `config-view.test.mjs` | 配置页**不整页重绘**（否则正在输入的文本框被换掉、焦点丢失） |
| `api-client.test.mjs` | 开发态 fetch 回退与 bridge 路径**行为等价**；响应解包契约 |
| `routing.test.mjs` | 视图 ↔ `location.hash` 双向同步；非法 hash 不得白屏 |
| `asset-contract.test.mjs` | index.html 引用的文件都存在、导航与 VIEWS 一致、两份 logo 同步 |

## 两条约定

**1. 被测模块必须能在不启动页面的前提下 import。**

`app.js` 末尾有一道守卫：

```js
if (!globalThis.__DENIA_NO_AUTOBOOT__) boot();
```

置了 `__DENIA_NO_AUTOBOOT__` 就只加载模块、不挂视图。之所以加这道口子而不是
把 `unwrapResponse` / `viewFromHash` 拆去别的文件：这些函数只服务于 app.js 这一处
流程，拆出去反而多一层间接。

需要多份「全新」模块实例时用 query 参数破缓存（ESM 按完整 URL 缓存）：

```js
const url = new URL("../../pages/denia/app.js", import.meta.url);
url.searchParams.set("v", "1");   // 换个字符串就是一份新实例
const { api } = await import(url.href);
```

**2. 桩要如实反映两种宿主的差异，不要为了省事合成一个。**

`api-client.test.mjs` 里 astrbot 与 quart 两条路径用的是**不同的** request 形态
（一个是带 `default` 形参的方法，一个是 async property）。曾经图省事合成一个，
结果另一条路径先撞 `TypeError`、再被兼容层的兜底分支吞掉，测试照样全绿，
而真正要验的代码一行都没执行。Python 侧的 `test_web_compat.py` 同理，
并且它当场就抓出了兼容层 legacy 分支的一个真 bug（`await request.json`
漏了括号，方法形态下必然 TypeError）。
