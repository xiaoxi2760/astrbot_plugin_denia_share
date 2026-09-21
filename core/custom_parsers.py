"""用户自定义解析器：从数据目录动态加载。

## 为什么是「Python 文件目录」而不是「配置驱动」

两者能力差得很远。配置驱动（填域名 + 取值规则）只能覆盖**静态 HTML 或直接返回
JSON** 的站点；而真实站点经常需要「先访问首页拿 cookie / token，再带着它调接口」
这种多步流程（雪球、今日头条都是），一张表表达不了。动态加载 `.py` 文件则是完整的
解析器能力 —— 本仓已有的 ``BaseParser`` + ``@handle`` 注册机制可以直接复用。

代价是使用者要会写 Python，且**放进来的文件会被直接执行**（见下面的安全边界）。

## 安全边界

- 目录固定在插件数据目录下的 ``custom_parsers/``，**刻意不做成配置项**：
  可配置就等于把「执行任意代码」变成一个可以在网页上改的字段。
- 只加载该目录下第一层的 ``*.py``（不递归子目录），跳过 ``_`` 开头。
- 单文件大小上限 ``MAX_FILE_BYTES``，防止误放大文件。
- 加载失败**不抛异常**，只记进 ``errors`` 并出现在 WebUI 里 —— 用户写错了要能
  自己看到原因，而一个文件写错不能拖垮插件、也不能拖垮别的自定义解析器。

**这里不做沙箱**（在进程内 exec 的 Python 代码没法可靠沙箱化）。放进这个目录的
文件权限等同于插件自身，请只放自己写的或看懂的代码。

## 接口版本

本仓的 ``BaseParser`` 接口会演进（历史上 ``create_gif`` 就被删过）。自定义文件必须
声明 ``PARSER_API_VERSION``，与 ``API_VERSION`` 一致才加载。这样接口变更后旧文件会
**明确报错**，而不是加载进来后在某个角落里以奇怪的方式失败。

## 说明文档去哪了

用法、冲突规则、接口版本、加载失败原因与安全提示都写在 ``core/custom_parsers_guide.md``，
并在建目录时复制一份成 ``custom_parsers/README.md`` —— 用户打开那个目录就能看到，
不必回头翻插件 README。
"""

from __future__ import annotations

import hashlib
import inspect
import sys
from dataclasses import dataclass, field
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .base_parser import BaseParser
from .constants import BUILTIN_PLATFORM_KEYS, register_platform, unregister_platform
from .data import Platform, platform_of

# 自定义解析器要声明的接口版本。**改 BaseParser 的对外接口时把这个 +1**，
# 并在 CHANGELOG / 修复报告里说明改了什么，让用户知道要跟着改哪里。
API_VERSION = 1

SUBDIR_NAME = "custom_parsers"

# 单文件大小上限。正常解析器几 KB，512 KB 足够覆盖，同时挡住「误把数据集
# 或日志改名成 .py 丢进来」这类情况。
MAX_FILE_BYTES = 512 * 1024

# 模板文件名。用 ``.py.txt`` 后缀，这样它**不会**被扫描到（扫的是 ``*.py``），
# 用户复制一份改名即可。
TEMPLATE_NAME = "TEMPLATE.py.txt"

# 说明文档：随模板一起写进数据目录，用户打开目录就能看到用法、冲突规则与安全提示。
# 正文放仓库里（``core/custom_parsers_guide.md``）便于阅读与维护，运行时复制一份过去。
# 插件的 README 只在「自定义解析器」一节里指路，把细节留在离使用现场最近的地方。
GUIDE_NAME = "README.md"
GUIDE_SOURCE = Path(__file__).with_name("custom_parsers_guide.md")

_TEMPLATE = '''"""自定义解析器模板 —— 复制本文件为 ``my_site.py``（去掉 .txt）即可被加载。

改完在插件 WebUI 的「解析 → 自定义解析器」里点「重新加载」，无需重启插件。
目录里的 ``README.md`` 有完整说明（冲突规则、接口版本、加载失败原因、安全提示）。
"""

# 必须声明，且与本插件的接口版本一致（当前为 1）。
# 不声明或对不上时不会加载，WebUI 里会显示原因。
PARSER_API_VERSION = 1

# 平台正式名：出现在 WebUI 平台列表、解析记录筛选与日志里。
PLATFORM_NAME = "示例站"
# 卡片与聊天消息里的叫法，留空 = 同正式名。
PLATFORM_CARD_NAME = ""

from astrbot_plugin_denia_share.core.base_parser import BaseParser, handle
from astrbot_plugin_denia_share.core.data import platform_of


class ExampleParser(BaseParser):
    # 平台键：小写英文，会出现在「禁用的平台」配置项与解析记录里。
    # 这里写什么键，PLATFORM_NAME 就会注册成这个键的展示名。
    platform = platform_of("example")

    # handle 的第一个参数是「关键词」，链接里必须包含它才会走到这个处理器
    # （比正则快，也让 `search_url` 能早退）。第二个参数是匹配链接的正则。
    @handle("example.com", r"example\\.com/(?:post|p)/(?P<pid>\\d+)")
    async def _parse(self, searched):
        pid = searched.group("pid")
        url = f"https://example.com/post/{pid}"

        # 自建 httpx 客户端**必须**走 client_kwargs：它会带上超时、证书校验开关
        # 与全局代理。裸建 AsyncClient 会让「只在环境变量里配代理」的部署静默直连。
        async with self.new_client() as client:
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()
            html = response.text

        # 下载媒体交给 self.downloader，返回值直接塞进 create_* 即可 ——
        # 它们接受「URL 字符串」或「下载任务」，进度与并发由下载器统一管。
        return self.result(
            url=url,
            title="标题",
            text="正文",
            author=self.create_author("作者名", "https://example.com/avatar.jpg"),
            contents=[self.create_image("https://example.com/1.jpg")],
        )
'''


@dataclass
class CustomParserEntry:
    """一个成功加载的自定义解析器。"""

    file: str
    key: str
    display_name: str
    card_name: str
    keywords: list[str]
    parser: Any = None
    error: str = ""
    # 在「禁用的平台」里。仍然出现在列表里（要能重新启用），但不实例化 ——
    # 用户代码的 __init__ 不该在禁用状态下被执行。
    disabled: bool = False


@dataclass
class CustomParserError:
    """一个加载失败的文件。``reason`` 会原样显示在 WebUI 里。"""

    file: str
    reason: str


@dataclass
class LoadResult:
    parsers: dict[str, Any] = field(default_factory=dict)
    entries: list[CustomParserEntry] = field(default_factory=list)
    errors: list[CustomParserError] = field(default_factory=list)

    @property
    def keys(self) -> set[str]:
        return set(self.parsers)


class CustomParserLoader:
    """扫描并加载 ``custom_parsers/`` 下的用户解析器。

    生命周期：``reload(downloader)`` 可以反复调用（保存配置 / 手动重载都会走），
    每次都会先把上一批注册过的东西撤干净，再重新扫描 —— 否则
    ``BaseParser.__init_subclass__`` 的自动注册会让类在注册表里越积越多。
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.result = LoadResult()
        self._registered_keys: list[str] = []
        self._registered_classes: list[type] = []

    # ---------- 目录 ----------

    def ensure_directory(self) -> None:
        """建目录，并放一份说明与模板（两者都只在缺失时写，不覆盖用户改过的）。"""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(
                f"[denia_share] 无法创建自定义解析器目录 {self.directory}", exc_info=True
            )
            return
        template = self.directory / TEMPLATE_NAME
        if not template.exists():
            try:
                template.write_text(_TEMPLATE, encoding="utf-8")
            except OSError:
                logger.warning(
                    f"[denia_share] 无法写入自定义解析器模板 {template}", exc_info=True
                )
        guide = self.directory / GUIDE_NAME
        if not guide.exists():
            self._copy_guide(guide)

    def _copy_guide(self, target: Path) -> None:
        """把仓库里的说明文档复制进数据目录。

        文档缺失（例如打包漏了它）**不致命**：目录与模板照常可用，只是目录里没有这份说明，
        所以只打一条警告，不让整个自定义解析器功能因此起不来。
        """
        try:
            content = GUIDE_SOURCE.read_text(encoding="utf-8")
        except OSError:
            logger.warning(
                f"[denia_share] 找不到自定义解析器说明文档 {GUIDE_SOURCE}，跳过"
            )
            return
        try:
            target.write_text(content, encoding="utf-8")
        except OSError:
            logger.warning(
                f"[denia_share] 无法写入自定义解析器说明 {target}", exc_info=True
            )

    # ---------- 加载 ----------

    def reload(self, downloader: Any, skip_keys: frozenset[str] | set[str] = frozenset()) -> LoadResult:
        """重新扫描目录。**永不抛异常** —— 任何问题都变成 errors 里的一条。

        ``skip_keys`` 是「禁用的平台」集合：命中的**仍然加载并注册元信息**
        （WebUI 要能显示它的名字与状态），但**不实例化** —— 禁用状态下不该执行
        用户代码的 ``__init__``。
        """
        self._purge()
        result = LoadResult()
        self.result = result
        self.ensure_directory()

        try:
            files = sorted(
                path
                for path in self.directory.iterdir()
                if path.is_file()
                and path.suffix == ".py"
                and not path.name.startswith("_")
            )
        except OSError:
            logger.warning(
                f"[denia_share] 无法读取自定义解析器目录 {self.directory}", exc_info=True
            )
            return result

        for path in files:
            try:
                entry, cls = self._load_file(path)
            except Exception as error:  # noqa: BLE001 —— 用户代码什么都可能抛
                result.errors.append(
                    CustomParserError(path.name, f"{type(error).__name__}: {error}")
                )
                logger.warning(
                    f"[denia_share] 自定义解析器 {path.name} 加载失败：{error}"
                )
                continue

            if entry.key in skip_keys:
                entry.disabled = True
                result.entries.append(entry)
                continue

            # 实例化失败同样只记一条：__init__ 是用户代码
            try:
                entry.parser = cls(downloader)
            except Exception as error:  # noqa: BLE001
                self._drop_classes([cls])
                result.errors.append(
                    CustomParserError(path.name, f"实例化失败 {type(error).__name__}: {error}")
                )
                logger.warning(
                    f"[denia_share] 自定义解析器 {path.name} 实例化失败：{error}"
                )
                continue

            result.parsers[entry.key] = entry.parser
            result.entries.append(entry)

        if result.entries or result.errors:
            logger.info(
                f"[denia_share] 自定义解析器：加载 {len(result.parsers)} 个"
                f"（{', '.join(result.parsers) or '无'}）"
                f"，禁用 {sum(1 for e in result.entries if e.disabled)} 个"
                f"，失败 {len(result.errors)} 个"
            )
        return result

    def _load_file(self, path: Path) -> tuple[CustomParserEntry, type]:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ValueError(
                f"文件过大（{size / 1024:.0f} KB > {MAX_FILE_BYTES // 1024} KB）"
            )

        source = path.read_text(encoding="utf-8")
        # 模块名带内容哈希：同一个文件改过之后重新加载不会复用旧的字节码缓存，
        # 同时避免两个同名文件（不同目录）互相覆盖。
        digest = hashlib.md5(source.encode("utf-8")).hexdigest()[:8]
        module_name = f"denia_custom_{path.stem}_{digest}"
        spec = spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ValueError("无法为该文件建立模块（路径或后缀不对？）")

        module = module_from_spec(spec)
        # __init_subclass__ 在 exec 的那一刻就把类 append 进了 BaseParser._registry，
        # 而后面任何一步校验都可能失败。**失败时必须把它摘掉** —— 否则一个写错的
        # 文件也会在注册表里留下痕迹，反复重载越积越多（2026-09-21 实测：
        # 几个坏文件就让 registry 从 9 涨到 11）。注册表只 append，所以新增的
        # 一定在尾部，按长度截断即可。
        registry_before = len(BaseParser._registry)
        try:
            spec.loader.exec_module(module)
            return self._build_entry(module, module_name, path)
        except BaseException:
            del BaseParser._registry[registry_before:]
            raise
        finally:
            # 不留在 sys.modules 里：这些是用户文件，留着只会让模块表随重载次数增长
            sys.modules.pop(module_name, None)

    def _build_entry(
        self, module: Any, module_name: str, path: Path
    ) -> tuple[CustomParserEntry, type]:
        """校验模块内容并注册平台元信息。抛出的异常由 ``_load_file`` 统一收成一条错误。"""
        declared = getattr(module, "PARSER_API_VERSION", None)
        if declared is None:
            raise ValueError(
                f"缺少 PARSER_API_VERSION（本插件当前接口版本为 {API_VERSION}），"
                "请参考目录里的 TEMPLATE.py.txt"
            )
        if declared != API_VERSION:
            raise ValueError(
                f"接口版本不匹配：文件声明 {declared}，本插件为 {API_VERSION}。"
                "插件更新后接口可能已变，请对照 TEMPLATE.py.txt 调整"
            )

        candidates = [
            obj
            for _, obj in inspect.getmembers(module, inspect.isclass)
            if issubclass(obj, BaseParser)
            and obj is not BaseParser
            # 只认本文件里定义的类：模块里 import 进来的内置解析器不算
            and obj.__module__ == module_name
        ]
        if not candidates:
            raise ValueError("文件里没有 BaseParser 的子类")
        if len(candidates) > 1:
            names = "、".join(sorted(cls.__name__ for cls in candidates))
            raise ValueError(
                f"一个文件只能定义一个解析器类，发现 {len(candidates)} 个（{names}）"
            )
        cls = candidates[0]

        platform = getattr(cls, "platform", None)
        if not isinstance(platform, Platform):
            raise ValueError(
                "类里缺少 platform（应写成 platform = platform_of(\"你的平台键\")）"
            )
        key = platform.name
        if not key:
            raise ValueError("platform 的键为空")
        if key in BUILTIN_PLATFORM_KEYS:
            raise ValueError(f"平台键 {key} 与内置平台重名，请换一个")
        if key in self.result.parsers or key in self._registered_keys:
            raise ValueError(f"平台键 {key} 与已加载的解析器重复")

        keywords = [keyword for keyword, _ in getattr(cls, "_key_patterns", [])]
        if not keywords:
            raise ValueError("类里没有任何 @handle，无法匹配链接")

        display_name = str(getattr(module, "PLATFORM_NAME", "") or key)
        card_name = str(getattr(module, "PLATFORM_CARD_NAME", "") or "")

        register_platform(key, display_name, card_name)
        self._registered_keys.append(key)
        self._registered_classes.append(cls)
        # 类定义时 platform_of(key) 还没注册元信息，展示名是键名本身；
        # 现在元信息到位了，重新取一次，让 cls.result() 里的 platform 正确。
        cls.platform = platform_of(key)

        entry = CustomParserEntry(
            file=path.name,
            key=key,
            display_name=display_name,
            card_name=card_name,
            keywords=keywords,
        )
        return entry, cls

    # ---------- 反注册 ----------

    def _purge(self) -> None:
        """撤掉上一批注册的平台元信息与解析器类。"""
        for key in self._registered_keys:
            unregister_platform(key)
        self._registered_keys = []
        self._drop_classes(self._registered_classes)
        self._registered_classes = []

    @staticmethod
    def _drop_classes(classes: list[type]) -> None:
        """从 BaseParser 的自动注册表里摘掉这些类。

        ``__init_subclass__`` 会在类定义时就 append 进 ``_registry``，而加载失败
        （比如没有 @handle）时我们已经执行过类定义了 —— 不摘掉的话，一个写错的
        文件也会在注册表里留下痕迹，重载几次就积一堆。
        """
        for cls in classes:
            try:
                BaseParser._registry.remove(cls)
            except ValueError:
                pass
