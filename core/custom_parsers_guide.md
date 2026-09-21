# 自定义解析器

这个目录里的 `*.py` 会被插件加载成新的解析平台 —— 想加第 12 个平台（或者改掉某个内置平台
的行为），不用改插件代码，把文件放在这里就行。

> 用法看本文件；**模板**是同目录的 `TEMPLATE.py.txt`，复制一份、去掉 `.txt` 后缀即可。
> 本文件是说明文档，**不会被加载**（插件只扫 `*.py`）。

## 怎么用

1. 复制 `TEMPLATE.py.txt` 为 `my_site.py`（**必须以 `.py` 结尾**；`_` 开头的文件会被跳过）
2. 改里面的平台键、平台名、`@handle` 正则与解析逻辑
3. 回插件网页界面「**解析 → 自定义解析器**」点 **「重新加载」** —— 立刻生效，不用重启插件
   （保存配置、切换平台开关也会顺带重新扫描一次目录）
4. 新平台会出现在「平台开关」里，可以像内置平台一样单独启停
5. 之后**群里直接发链接就会被解析**：卡片、解析记录、平台开关都和内置平台走同一条链路

删掉文件、或改了平台键之后点一次「重新加载」，旧实例会被一起卸掉 —— 不会出现
「文件删了但那个平台还在解析」。

## 和内置平台冲突时谁优先

内置平台的链接**优先由内置平台处理**：写一个匹配 `bilibili.com` 的自定义解析器，
只要 B站 还在启用状态，链接就还是会走内置那条。想让它接管，先进「平台开关」把对应的
内置平台**关掉**。

平台键也不能与内置的 11 个重名（`bilibili` / `douyin` / `kuaishou` / `weibo` /
`xiaohongshu` / `twitter` / `nga` / `acfun` / `github` / `pixiv` / `steam`），
重名会被拒绝加载；要顶替内置平台的行为，请另起一个键（如 `bilibili_hd`）。

## 一个最小的例子

```python
PARSER_API_VERSION = 1        # 必须声明，见下
PLATFORM_NAME = "示例站"       # 出现在平台列表与解析记录里
PLATFORM_CARD_NAME = ""       # 卡片里的叫法，留空 = 同正式名

from astrbot_plugin_denia_share.core.base_parser import BaseParser, handle
from astrbot_plugin_denia_share.core.data import platform_of


class ExampleParser(BaseParser):
    # 平台键：小写英文。会出现在「禁用的平台」配置与解析记录里。
    platform = platform_of("example")

    # handle 的第一个参数是「关键词」：链接里必须包含它才会走到这个处理器
    # （比正则快，也让 search_url 能早退）。第二个参数是匹配链接的正则。
    @handle("example.com", r"example\.com/(?:post|p)/(?P<pid>\d+)")
    async def _parse(self, searched):
        pid = searched.group("pid")
        url = f"https://example.com/post/{pid}"

        # 自建 httpx 客户端**必须**走 self.new_client()（或 self.client_kwargs()）：
        # 它会带上超时、证书校验开关与全局代理。裸建 AsyncClient 会让
        # 「只在环境变量里配代理」的部署静默直连。
        async with self.new_client() as client:
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()

        # 下载媒体交给 self.downloader，返回值直接塞进 create_* 即可 ——
        # 它们接受「URL 字符串」或「下载任务」，进度与并发由下载器统一管。
        return self.result(
            url=url,
            title="标题",
            text="正文",
            author=self.create_author("作者名", "https://example.com/avatar.jpg"),
            contents=[self.create_image("https://example.com/1.jpg")],
        )
```

可用的构建件与内置解析器完全一样：`self.result()` / `self.create_author()` /
`self.create_video()` / `self.create_image()` / `self.create_images()` /
`self.create_audio()` / `self.new_client()` / `self.client_kwargs()` /
`self.get_redirect_url()` / `self._add_limit_warning()`，以及
`ParseException`（解析失败，会换下一条路径）与 `IgnoreException`（策略跳过，不算失败）。

照着 `core/parsers/github.py` 或 `core/parsers/pixiv.py` 抄最快 —— 这两个最短。

## 接口版本

文件里必须声明 `PARSER_API_VERSION`，且与插件当前的接口版本一致（**当前为 1**）。
不一致时**拒绝加载**并在页面上说明原因，而不是加载进来后在某个角落里以奇怪的方式失败。

插件的 `BaseParser` 接口会随版本演进（历史上 `create_gif` 就被删过）。升级插件之后如果
文件突然不加载了，先看页面上的提示 —— 多半是接口版本变了，对着 `TEMPLATE.py.txt` 改一下即可。

## 加载失败会怎样

**一个文件写错不会拖垮插件，也不会影响别的自定义解析器。** 每个文件独立加载，
下面每一种都会被拒绝，原因原样显示在「自定义解析器」卡片里（红框），**不需要去翻服务器日志**：

- 语法错
- 缺 `PARSER_API_VERSION`，或声明版本与插件不一致
- 没有 `BaseParser` 的子类，或一个文件里定义了多个解析器类
- 类里没有 `platform`，或没有任何 `@handle`
- 平台键与内置平台重名、与已加载的解析器重复
- 文件超过 512 KB

失败的照上面报错，**成功的那些照常工作**。

## ⚠️ 安全提示

**这个目录里的文件会被直接执行**，权限等同于插件自身。插件不做沙箱（在进程内 exec 的
Python 代码没法可靠沙箱化），所以：

- 只放**自己写的**、或**完整读过、看懂的**代码
- 不要从网上随手抄一段来路不明的解析器丢进来
- 这个目录刻意**不做成配置项** —— 可配置就等于把「执行任意代码」变成一个能在网页上改的字段

另外，`__init__` 里不要做网络请求：加载会在保存配置、切换平台时反复触发。
