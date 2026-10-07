"""演示数据：不联网也能看到完整界面效果。标题均为模板生成的虚构内容，界面上会标注“演示数据”。"""

from __future__ import annotations

import random
import time

POS = [
    "{n}三季度净利润同比增长{p}%，业绩超预期",
    "{n}获多家券商上调目标价，维持买入评级",
    "{n}宣布{b}亿元回购计划，彰显发展信心",
    "北向资金连续三日净买入{n}",
    "{n}新品发布会火爆，订单饱满",
    "{n}股价大涨{q}%，创历史新高",
    "{n}与头部企业签署战略合作协议",
]
NEG = [
    "{n}收到交易所问询函，要求说明业绩下滑原因",
    "{n}大股东拟减持不超过{q}%股份",
    "{n}股价跳水，盘中一度跌超{q}%",
    "{n}被曝产品质量问题，消费者投诉增多",
    "机构下调{n}评级，担忧需求疲软",
    "{n}子公司涉及诉讼，涉案金额{b}亿元",
]
NEU = [
    "{n}召开{y}年度股东大会",
    "{n}发布关于董事会换届的公告",
    "{n}将于下周参加行业交流会",
    "关注｜{n}最新调研纪要要点整理",
    "{n}今日成交额{b}亿元，换手率{q}%",
]
MARKET = [
    ("央行宣布降准0.5个百分点，释放长期资金约1万亿元", "pos"),
    ("沪指收涨1.2%，两市成交额重回万亿", "pos"),
    ("创业板指低开低走，半导体板块领跌", "neg"),
    ("统计局：9月CPI同比上涨0.4%", "neu"),
    ("美联储维持利率不变，市场关注后续指引", "neu"),
    ("多家上市公司发布业绩预告，预增公司占比过半", "pos"),
    ("某地产企业债务逾期，相关债券暴跌", "neg"),
    ("证监会：严厉打击财务造假等违法违规行为", "neg"),
    ("新能源车9月销量同比增长35%", "pos"),
    ("原油价格大幅下跌，能源股承压", "neg"),
]


def seed_demo(app, days=14, per_entity=26, seed=None) -> int:
    rnd = random.Random(seed)
    store = app.store
    src = {"id": None, "name": "演示数据"}
    now = int(time.time())
    items = []
    for e in store.entities(enabled_only=True):
        for _ in range(per_entity):
            # 越近的日期资讯越多，末两天给某个对象制造一点负面高峰
            age = int(rnd.triangular(0, days * 86400, 0))
            pool = rnd.choices([POS, NEG, NEU], weights=[4, 3, 4])[0]
            title = rnd.choice(pool).format(n=e["name"], p=rnd.randint(8, 60), b=rnd.randint(2, 80), q=rnd.randint(1, 9), y=time.localtime().tm_year)
            items.append({
                "title": title + f"（演示{rnd.randint(1000, 9999)}）",
                "summary": "这是一条演示数据，用于预览界面效果，并非真实新闻。",
                "url": "", "media": rnd.choice(["证券时报", "上海证券报", "第一财经", "财新", "21世纪经济报道", "界面新闻"]),
                "published_at": now - age, "entity_ids": [e["id"]],
            })
    for _ in range(days * 4):
        title, _lab = rnd.choice(MARKET)
        items.append({
            "title": title + f"（演示{rnd.randint(1000, 9999)}）", "summary": "演示数据，并非真实新闻。", "url": "",
            "media": rnd.choice(["新华社", "财联社", "东方财富"]), "published_at": now - rnd.randint(0, days * 86400),
        })
    new = app.collector.ingest(items, src)
    return len(new)
