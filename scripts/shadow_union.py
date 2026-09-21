#!/usr/bin/env python3
"""Shadow comparison: for the same batch of real messages, "per-template judging" vs "union judging" in terms of consistency and usage.

Comparison items:
- per (message × template) hit consistency (target 100%)
- number of Jev calls and real token usage (union judging should be markedly cheaper)

Usage:
    python scripts/shadow_union.py [channel] [count]     # defaults to Financial_Express 12

TYPESAFE_API_KEY is read from the environment or .env; without it the script exits immediately.
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
from tgfilter.judging import build_groups, judge_post, template_fingerprint  # noqa: E402
from tgfilter.models import Template  # noqa: E402

load_dotenv()

CHINA_Q = {"type": "noul", "title": "中国相关",
           "instructions": "Is this message directly related to China (mainland, Hong Kong, Macau or Taiwan) in terms of markets, policy, companies, industries or the economy?"}
CRYPTO_Q = {"type": "noul", "title": "加密货币",
            "instructions": "Does this message directly involve cryptocurrencies, Bitcoin, Ethereum, or digital asset regulation?"}
COMMODITY_Q = {"type": "noul", "title": "大宗商品",
               "instructions": "Does this message directly involve prices or supply/demand of commodities such as crude oil, gold, copper, or agricultural products?"}
TECH_Q = {"type": "noul", "title": "AI/科技",
          "instructions": "Does this message directly involve AI, semiconductors, or major tech companies' products, investments, or regulation?"}


def _tpl(name: str, qid: str, question: dict, threshold: float = 0.7) -> Template:
    return Template.model_validate({
        "name": name,
        "questions": {qid: question},
        "match": {"logic": "all",
                  "conditions": [{"question": qid, "op": ">=", "value": threshold}]},
    })


# Simulate several subscription templates coexisting on one channel (the China question is shared across templates to verify question-level dedup)
TEMPLATES = [
    _tpl("中国财经", "china", CHINA_Q),
    _tpl("加密货币", "crypto", CRYPTO_Q),
    _tpl("大宗商品", "commodity", COMMODITY_Q),
    _tpl("AI 科技", "tech", TECH_Q),
]


async def main() -> None:
    channel = sys.argv[1] if len(sys.argv) > 1 else "Financial_Express"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    settings = Settings.load()
    if not settings.typesafe_api_key:
        print("⏭ 未配置 TYPESAFE_API_KEY。")
        return

    async with httpx.AsyncClient() as http:
        fetcher = ChannelFetcher(http, page_delay=0.5)
        info = await fetcher.head(channel)
        posts, _ = await fetcher.fetch_since(channel, max(1, info.head_id - 60))
        posts = [p for p in posts if p.text][-limit:]
        print(f"频道 @{channel}｜样本 {len(posts)} 条 × 模板 {len(TEMPLATES)} 个\n")

        jev = JevClient(http, settings.typesafe_api_key,
                        settings.typesafe_base_url, concurrency=8)

        # ---- separate judging (legacy behavior): one call per message × template
        legacy: dict[tuple[int, str], bool] = {}
        legacy_in = legacy_out = 0
        legacy_calls = 0
        for tpl in TEMPLATES:
            results = await jev.classify_many([p.text for p in posts], tpl)
            for post, result in zip(posts, results):
                usage = result.get("usage") or {}
                legacy_in += int(usage.get("input_tokens") or 0)
                legacy_out += int(usage.get("output_tokens") or 0)
                legacy_calls += 1
                if result.get("error"):
                    continue
                legacy[(post.id, tpl.name)] = bool(tpl.evaluate(result["answers"]))

        # ---- union judging: union of all template questions, one call per message
        fps = {template_fingerprint(tpl): tpl for tpl in TEMPLATES}
        groups = build_groups(fps)
        union: dict[tuple[int, str], bool] = {}
        union_in = union_out = 0
        union_calls = failed = 0
        for post in posts:
            judged = await judge_post(jev, post.text, groups)
            union_calls += judged.calls
            union_in += judged.input_tokens
            union_out += judged.output_tokens
            if not judged.payload:
                failed += 1
                continue
            for fp, tpl in fps.items():
                answers = judged.payload.get(fp)
                if answers is not None:
                    union[(post.id, tpl.name)] = bool(tpl.evaluate(answers))

        # ---- comparison
        total = agree = 0
        mismatches: list[str] = []
        for key, legacy_hit in sorted(legacy.items()):
            if key not in union:
                continue
            total += 1
            if union[key] == legacy_hit:
                agree += 1
            else:
                mismatches.append(f"  ✗ #{key[0]} {key[1]}: 分开={legacy_hit} 联合={union[key]}")
        pct = (agree / total * 100) if total else 0.0
        print(f"判定一致性：{agree}/{total}（{pct:.1f}%）"
              + (f"｜失败消息 {failed} 条" if failed else ""))
        for line in mismatches[:10]:
            print(line)
        print(f"\n调用次数：分开 {legacy_calls} → 联合 {union_calls}"
              f"（节省 {100 * (1 - union_calls / max(legacy_calls, 1)):.0f}%）")
        print(f"input tokens：分开 {legacy_in:,} → 联合 {union_in:,}"
              f"（节省 {100 * (1 - union_in / max(legacy_in, 1)):.0f}%）")
        print(f"output tokens：分开 {legacy_out:,} → 联合 {union_out:,}")


if __name__ == "__main__":
    asyncio.run(main())
