#!/usr/bin/env python3
"""Smoke test: no bot token needed — really fetches the t.me preview and (optionally) really calls Jev for classification.

Usage:
    python scripts/smoke_live.py [channel name]

TYPESAFE_API_KEY is read from the environment or .env; the Jev part is skipped when it is not configured.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from tgfilter.channel_fetch import ChannelFetcher  # noqa: E402
from tgfilter.config import Settings  # noqa: E402
from tgfilter.jev import JevClient  # noqa: E402
from tgfilter.models import Template  # noqa: E402

load_dotenv()

TEMPLATE = Template.model_validate({
    "name": "中国关注",
    "questions": {
        "china": {"type": "noul", "title": "相关",
                  "instructions": "这条消息是否与中国（含港澳台）的市场、政策、公司、"
                                  "行业或经济直接相关？",
                  "criteria": {"true": "内容涉及中国相关主题",
                               "false": "纯海外内容，不涉及中国"}},
        "importance": {"type": "score", "title": "重要",
                       "instructions": "这条消息对关注金融市场的人有多重要？",
                       "criteria": ["日常资讯", "一般", "较高", "重大"]},
    },
    "match": {"logic": "all",
              "conditions": [{"question": "china", "op": ">=", "value": 0.7},
                             {"question": "importance", "op": ">=", "value": 1.5}]},
})


async def main() -> None:
    channel = sys.argv[1] if len(sys.argv) > 1 else "Financial_Express"
    settings = Settings.load()
    async with httpx.AsyncClient() as http:
        fetcher = ChannelFetcher(http, page_delay=0.5)

        info = await fetcher.head(channel)
        print(f"✅ 频道：{info.title}（@{channel}）｜head={info.head_id}"
              f"｜最近 {len(info.posts)} 条")

        posts, cursor = await fetcher.fetch_since(channel, max(1, info.head_id - 40))
        posts = [p for p in posts if p.text][-20:]
        print(f"✅ 续抓：{len(posts)} 条（游标 → {cursor}）")

        if not settings.typesafe_api_key:
            print("⏭  未配置 TYPESAFE_API_KEY，跳过 Jev 判定。")
            return

        jev = JevClient(http, settings.typesafe_api_key,
                        settings.typesafe_base_url, concurrency=8)
        results = await jev.classify_many([p.text for p in posts], TEMPLATE)
        hits = 0
        for post, result in zip(posts, results):
            if result.get("error"):
                print(f"   [ERR] {result['error'][:80]}")
                continue
            ok = TEMPLATE.evaluate(result["answers"])
            hits += int(ok)
            values = result["answers"]
            print(f"   [{'✔' if ok else ' '}] "
                  f"china={values.get('china', {}).get('noul')} "
                  f"imp={values.get('importance', {}).get('score')}"
                  f"｜{post.text[:42]}")
        print(f"✅ Jev：命中 {hits}/{len(posts)}")


if __name__ == "__main__":
    asyncio.run(main())
