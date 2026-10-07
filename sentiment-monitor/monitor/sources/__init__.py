"""数据源注册表。新增数据源：写一个 Source 子类，加到 REGISTRY 即可。"""

from .base import Source
from .cn_finance import ClsTelegraphSource, EastmoneyFastSource, EastmoneySearchSource, SinaRollSource
from .rss import BingNewsSource, GoogleNewsSource, RssSource, YahooFinanceSource

REGISTRY: dict[str, Source] = {
    cls.type: cls()
    for cls in (
        SinaRollSource,
        EastmoneyFastSource,
        ClsTelegraphSource,
        EastmoneySearchSource,
        BingNewsSource,
        GoogleNewsSource,
        YahooFinanceSource,
        RssSource,
    )
}


def get(type_: str) -> Source:
    if type_ not in REGISTRY:
        raise KeyError(f"未知数据源类型：{type_}")
    return REGISTRY[type_]


def catalog():
    return [s.info() for s in REGISTRY.values()]
