# B站接口响应 fixture

这些是 B站接口的真实响应，经消毒后用于 `tests/test_bili_fallback_*.py` 的
离线测试。采集于 2026-10-06，采集器为 yaya 侧的 `_probe_bili_live.py`。

## 消毒做了什么

只替换 URL 里的 host（`*.bilivideo.cn` → `cdnN.bilivideo.test`、
`*.hdslb.com` → `imgN.bilibili.test`），**保留路径与查询参数**。
真实 CDN 地址带签名凭据，不该进 git 历史。

**结构与数值全部原样保留** —— codecid、bandwidth、id、`backupUrl`
的条数、`_meta` 都在，因为它们正是测试要断言的东西。

## 覆盖了什么

| 样本 | 场景 |
| --- | --- |
| `BV1GJ411x7h7` | 2017 老视频，HEVC sample entry 是 `hev1` |
| `BV1YtHs62EHZ` / `BV1fnHL68EPT` | 现代投稿（433s / 112min） |
| `BV1cwHa6mEPH` / `BV1wHh16yE8F` | 多 P（2P / 5P） |
| `av117370500878484` | av 号路径 |
| `opus1129722427782201345` | opus 图文（ARTICLE） |
| `opus1255560717556252676` | opus 无正文（DRAW） |

## 没覆盖什么

- **番剧 PGC**：`pgc/*` 接口匿名访问一律 `code=0` 但 `data={}`，
  发现侧接口也基本全被封，本机无登录态，采不到 ep 号样本
- **付费 / 会员内容**：需登录态
- **多段 durl**：8 样本 + 20+ 次参数变体实测恒为 1 段

## 关键实测结论（2026-10-06）

1. `x/player/wbi/playurl` **不校验 WBI 签名** —— 错误 `w_rid`、
   错误 `mixin_key`、过期 `wts` 全部返回 `code=0`
2. `x/v2/reply/wbi/main`（热评）**强制校验签名** —— 不带签名返回
   `-403 访问权限不足`；其非 wbi 版本 `x/v2/reply/main` 可用
3. HEVC sample entry 已从 `hev1` 换代为 `hvc1`
4. `playurl` 已不接受 `aid` 参数（-400），只能用 `bvid`
