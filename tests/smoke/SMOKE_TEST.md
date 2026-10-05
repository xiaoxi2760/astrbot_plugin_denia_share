# 冒烟测试 · 希望解析器（astrbot_plugin_denia_share）

对 11 个解析器做「能不能认链接 + 能不能解析出东西」的两层检查。
**这不是单元测试**——单元测试验逻辑，冒烟测试验「今天这台机器、这个网络下、
这个链接还能不能用」，两者都会坏，但坏的原因完全不同。

## 跑

```bash
# 走本地代理（默认 127.0.0.1:7897）
py -3 tests/smoke/run_smoke.py

# 直连
py -3 tests/smoke/run_smoke.py ""

# 只跑一个平台，例如 steam
py -3 tests/smoke/run_smoke.py "" steam
```

结果分层输出：**第一层派发**（离线、确定性）、**第二层解析**（联网）。

## 两层为什么分开

| | 第一层：派发 | 第二层：解析 |
|---|---|---|
| 联网 | 否 | 是 |
| 验什么 | URL 能否被**正确的**解析器认领 | 真的跑一遍 `parse()`，断言结构完整 |
| 确定性 | 完全确定，可进 CI | 受网络/风控/内容存活影响 |
| 典型失败 | `@handle` 正则写错、平台改名、遍历顺序变了 | 接口改版、Cookie 失效、内容被删 |

把两层混在一起是这类测试最常见的坏法：链接死了你在报告里看到"微博解析失败"，
修了一下午，最后发现是那条微博被删了。

## 判据

只断言**结构**，不比对具体文案：

- 派发到期望的解析器
- `result.title` 非空
- 至少解析出 1 个图片 / 视频 / 音频

**为什么不断言文案**：平台文案随时变，拿它当断言只会让测试变成随机失败，
真正坏了的时候你反而会忽略红字。

**负向用例**用 `expect_error="<理由片段>"` 声明：这条**应该**抛 ParseException 且
理由里含该片段。默认判据是「必须解析成功」，所以负向用例必须显式声明，否则会把
正确的拒绝当成失败。**反过来也守住了**——声明了 expect_error 却真的解析成功，
同样判失败：「非仓库路径被静默解析成仓库」这种行为，光靠「没抛异常」是抓不到的。

## 当前结果（2026-10-06，代理 127.0.0.1:7897）

**34 条用例，其中 16 条有 URL 可跑。9 条通过。**

### 通过

| 平台 | 内容类型 | URL | 结果 |
|---|---|---|---|
| AcFun | 视频 | `acfun.cn/v/ac43445963` | 视频=1 |
| GitHub | 仓库 | `github.com/xiaoxi2760/astrbot_plugin_denia_share` | 图=1 |
| GitHub | 非仓库路径应被拒 | `github.com/features/copilot` | 预期报错 ✓ |
| GitHub | 不存在的仓库应被拒 | `xiaoxi2760/definitely_not_exist_zzz9` | 预期报错 ✓ |
| Pixiv | 插画 | `pixiv.net/artworks/87070841` | 图=1 |
| Pixiv | 作品 | `pixiv.net/artworks/115779535` | 图=1 |
| Steam | 游戏商店页 | `store.steampowered.com/app/730/` | Counter-Strike 2 图=4 |
| Steam | DLC | `store.steampowered.com/sub/400/` | Portal 图=4 |
| Steam | 合集/包 | `store.steampowered.com/bundle/1145360/` | Hades 图=4 |

### 失败 —— 分三类，不要混为一谈

**A. 环境问题（不是插件 bug，换个环境可能就过）**

| 平台 | 现象 | 结论 |
|---|---|---|
| Steam ×3 | 派发 OK，解析时 `ConnectError` | **已定位并修复**（见下面「Steam 连不通的根因」）。修复后走代理 3/3 通过 |
| B站 ×2 | `TypeError: object _Any can't be used in 'await'` | **测试环境限制**：`bilibili_api` 是桩，`Video` 类的方法不是真的 async。B站要等搬到自建解析器才能离线跑 |
| 微博 ×3 | `ValidationError: Object missing required field 'data'` | 见下面「发现 3」——接口返回了错误 JSON |

**B. 用例设计问题（我配错了 URL）**

| 用例 | 现象 | 结论 |
|---|---|---|
| AcFun 文章 `a/ac37416587` | 派发到 acfun（`/ac\d+` 意外匹配到路径里的 id），解析时 302 | 插件按**视频页**处理，文章页会跳走。这条用例本身没意义 |
| AcFun 番剧 `bangumi/aa5023295` | 派发失败 | 符合预期，插件没注册 bangumi pattern。见「发现 2」 |

**C. 真问题（要改代码）** → 见下面「冒烟发现」

---

## Steam 连不通的根因（已修复）

**症状**：三条 Steam 用例全部 `ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]`。

**根因**：`C:\Windows\System32\drivers\etc\hosts` 里有 15 行把 Steam 域名指向 `127.0.0.1`：

```
127.0.0.1 store.steampowered.com      ← 解析器唯一依赖的域名
127.0.0.1 api.steampowered.com
127.0.0.1 steamcommunity.com
127.0.0.1 media.steampowered.com
... 共 15 行
```

Steam 客户端（或去广告/加速工具）会往 hosts 写这些行来屏蔽商店图片和社区页、
加速自己的 UI。结果 `store.steampowered.com` 解析到 `127.0.0.1`，443 端口直接连不通。

**一个容易误判的点**：当时**代理也失败**，看着像线路问题。其实走 HTTP 代理时
客户端把域名交给代理解析、本地 hosts 不该生效——真正的原因是 DNS 缓存里还留着
被污染的解析结果。`ipconfig /flushdns` 之后代理路径也一起好了。

**修复**：管理员 PowerShell 把那 15 行注释掉 + flushdns（备份在 `hosts.denia_bak`）：

```powershell
$hosts = "$env:SystemRoot\System32\drivers\etc\hosts"
Copy-Item $hosts "$hosts.denia_bak" -Force
$out = foreach ($l in [IO.File]::ReadAllLines($hosts)) {
  if ($l -match '(?i)^\s*127\.0\.0\.1\s+\S*(steam|steampowered)') { "# [denia] 已停用: $l" }
  else { $l }
}
[IO.File]::WriteAllLines($hosts, $out)
ipconfig /flushdns | Out-Null
```

**修复后**：走代理 **3/3 通过**（CS2 / Portal DLC / Hades 合集），直连 2/3
（DLC 那条直连仍不稳，走代理就正常）。

**注意两点**：

1. **Steam 客户端开着的话可能把行写回去。** 要长期保持得在 Steam 设置里关掉相关项。
2. **这个只影响本机。** 插件跑在服务器 / Docker / VPS 上的话，那台机器 hosts
   大概率是干净的，Steam 本来就正常——所以这条不该记成插件 bug。

**顺带一条排查经验**：`cheapshark.com`（Steam 价格史数据源，解析器还依赖它）
走同一条线路是通的，说明**只有 `store.steampowered.com` 这一个域名的线路有问题**。
下次遇到「同组域名有的通有的不通」，先把每个域名单独探一遍，别整体归因为「网络不通」。

---

## 冒烟发现

### 1. 微博最常见的分享形式匹配不到 🔴

```
m.weibo.cn/statuses/show?id=XXX   →  匹配失败
m.weibo.cn/detail/XXX             →  OK
m.weibo.cn/status/XXX             →  OK
weibo.com/<uid>/<wid>             →  OK
weibo.com/tv/show/...             →  OK
video.weibo.com/show?fid=...      →  OK
weibo.com/article/id/...          →  OK
```

原因在 `core/parsers/weibo.py:119`：

```python
@handle("m.weibo.cn", r"weibo\.cn/(?:status|detail|\d+)/(?P<wid>[0-9a-zA-Z]+)")
```

`status` 匹配不到 `statuses`（少个 `es`）。而 `m.weibo.cn/statuses/show?id=`
恰恰是微博 App「分享 → 复制链接」的默认形态，用户粘到群里的多半是这个。

**症状**：链接发进群里，插件不响应、也不报错（`_match_parser` 返回 None 就静默跳过）。

**建议**：pattern 改成 `weibo\.cn/(?:status(?:es)?|detail|\d+)/...`，
或者补一条 `@handle("m.weibo.cn", r"weibo\.cn/statuses/show\?id=(?P<wid>[0-9a-zA-Z]+)")`。
后者更稳（同时保住原有的路径形态）。

### 2. AcFun 番剧不支持（可能是有意的）

`@handle("acfun.cn", r"(?:ac=|/ac)(?P<acid>\d+)")` 只覆盖视频。
`bangumi/aa5023295` 这类番剧链接匹配不上。番剧还有地区限制，
即使补了 pattern 也要考虑「解析不了时给什么提示」。

**需要你确认**：AcFun 番剧算不算要支持的场景？

### 3. 微博接口返回错误 JSON 时，报错信息对用户是天书 🟡

实测无 Cookie 环境下，微博返回的是 **HTTP 200 + `Sina Visitor System` 挑战页**
或一个缺 `data` 字段的错误 JSON。插件的访客身份机制（`weibo.py:74-86`）和
content-type 检查（`weibo.py:227-229`）都做得对，但：

```python
weibo_data = self._decode_status(response.content, weibo_id)   # weibo.py:231
```

**这行没有 try**。msgspec 解码失败抛的 `ValidationError: Object missing required
field 'data'` 会直接穿透到用户面前。用户看到这句既不知道是风控、不知道该登录、
也不知道该等一下。

**建议**：给 `_decode_status` 包一层，把 msgspec 的 `ValidationError` 转成
`ParseException("微博接口返回了非预期数据（可能被风控），配了 Cookie 或稍后重试")`。
同文件里已经有 `raise ParseException(f"被风控拦截({code})…")` 这类可读文案的先例，
照着写即可。

### 4. GitHub 其实只有「仓库」一种内容类型 🟡

**实测**（同一个仓库，四种 URL）：

```
github.com/xiaoxi2760/astrbot_plugin_denia_share
github.com/xiaoxi2760/astrbot_plugin_denia_share/issues/1
github.com/xiaoxi2760/astrbot_plugin_denia_share/releases/tag/1.2.0
github.com/xiaoxi2760/astrbot_plugin_denia_share/blob/main/README.md
    → 四条产出完全相同：标题=仓库名，正文=仓库描述
```

原因在 `core/parsers/github.py:58`：

```python
@handle("github.com", r"github\.com/(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+)")
```

只捕获 owner/repo，**后面路径是什么完全不看**；而且只调
`/repos/{owner}/{repo}` 这一个接口（`github.py:70`），从来没请求过 releases 或 issues。

**所以我把原来那两条用例（Issue / Release）撤了——它们是假阳性**：
`gh-issue` 当时"通过"了，但测的其实是仓库解析，等于什么都没测到。
换成了两条真实存在的边界行为：

| 用例 | 期望 |
|---|---|
| `github.com/features/copilot` | `ParseException: 不是仓库地址: features/copilot` |
| 不存在的仓库 | `ParseException: 仓库不存在: owner/repo` |

github.com 下有大量非仓库路径（`features` / `orgs` / `apps` / `marketplace`…），
解析器已经用一个黑名单挡住了（`github.py:63-66`），这两条就是守住那个黑名单的。

**要不要补 Issue / Release 解析？** 取决于你想不想让用户在群里发
`.../releases/tag/1.2.0` 时看到的是**版本说明**而不是仓库卡片。目前的行为不算错
（解析器明确只认仓库），但用户很可能期望看到 release 内容——这是个产品决策，
不是 bug，我没擅自改。

### 5. B站离线测不了，本身就是个信号 🟡

B站解析器重度依赖 `bilibili_api`（`Video` 的 async 方法、扫码登录、类型常量），
桩一装就报 `can't be used in 'await'`。这不是测试框架的问题——
它说明**B站解析的可测性和它对第三方库的耦合程度成正比**。
搬到你自己的实现之后，这一层自然消失。

---

## 待补 URL（18 条，需要你提供）

我拿不到的都是需要登录态 / 无公开列表接口的平台。下面按平台列，**每条都是它需要验证的代码分支**。

### B站（2）
- [ ] 动态图文：`t.bilibili.com/<数字>` 或 `bilibili.com/opus/<数字>`，**要图文型**
- [ ] 专栏文章：`bilibili.com/read/cv<数字>`

### 抖音（3）
- [ ] 视频：`douyin.com/video/<数字>`
- [ ] 图文笔记：`douyin.com/note/<数字>` ← **必须是图文帖**
- [ ] 图集：`douyin.com/slides/<数字>`

### 快手（3）
- [ ] 视频：`v.kuaishou.com/xxxxx`
- [ ] 图集：**要图集不是视频** ← 这两种走的是完全不同的分支
- [ ] 极速版/快影：`chenzhongtech.com/fw/xxxxx`（没有可跳过）

### 小红书（3）
- [ ] 图文笔记：`xiaohongshu.com/explore/<24位hex>`
- [ ] 视频笔记：**要视频**
- [ ] 短链：`xhslink.com/xxxx`（走 `parse_with_redirect`，测重定向分支）

### Twitter（3）
- [ ] 纯文本/单图
- [ ] 视频推
- [ ] 多图推（2~4 张，测 `media_extended` 取流）

### NGA（3）
- [ ] 普通帖：`nga.178.com/read.php?tid=<数字>`
- [ ] **含图帖** ← 验证 `img.nga.178.com` 取图
- [ ] 长帖/讨论串

### Pixiv（1）
- [ ] ugoira 动图（`artworks/<数字>` 但类型是动图）

### 额外：微博（3，不计入上面 18 条）

这 3 条不是补覆盖，是**专门验「发现 1」的修复**：
- [ ] 一条真实的 `m.weibo.cn/statuses/show?id=` —— 修完 pattern 后应该能派发并解析
- [ ] 一条配了 Cookie 后的 `m.weibo.cn/detail/<wid>` —— 用来判断「报错是天书」那句
  到底是「没 Cookie 必然这样」还是「有 Cookie 也这样」，进而决定该不该无条件包一层提示
- [ ] 视频微博 `video.weibo.com/show?fid=...`

**拿到之后**：填进 `tests/smoke/cases.py` 对应的 `url=""`，`source` 改成 `manual`，
跑一次 `run_smoke.py` 即可。你也可以直接把链接贴给我，我填。

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `harness.py` | 脱离 AstrBot 单跑真实解析器。派发逻辑**照抄** `main.py::_match_parser` |
| `cases.py` | 34 条用例的数据表，`source` 字段标 URL 来源（api/doc/manual） |
| `run_smoke.py` | 跑测 + 分层报告 |
| `discover_urls.py` | 通过代理打平台公开接口捞当前存活的 ID（能找到的都标 `source=api`） |
| `../_stubs.py` | astrbot / bilibili_api 的桩 |

**harness 的派发逻辑是照抄 main.py 的，不是重写**——一旦两边分叉，
冒烟通过而线上派错解析器，测试就白做了。
