"""Adversarial repost and non-repost examples; no network or model calls."""

import pytest

from tgfilter.dedupe import canonicalize, is_duplicate

STORY = (
    "该项目今天公布完整调查结果，负责人说明了背景、时间线和采取的后续措施。"
    "多名参与者核对了公开记录，确认讨论的是同一件事，希望大家阅读原始材料。"
    "最终公告也列出了具体依据，后续进展会由主管部门统一发布。"
)
ENGLISH = (
    "The agency released its full investigation report today and described the "
    "timeline, evidence, and follow-up actions. Several independent reviewers "
    "checked the public record before the final announcement was published."
)


@pytest.mark.parametrize(("first", "second", "duplicate"), [
    pytest.param(STORY, STORY + "\n📮投稿 ☘️频道 🐧聊天 https://t.me/alpha",
                 True, id="copied-story-added-channel-footer"),
    pytest.param("【匿名投稿】" + STORY + "\n投稿请私信 https://t.me/alpha",
                 "【读者来稿】" + STORY + "\n欢迎订阅 https://t.me/beta",
                 True, id="different-headers-and-footers"),
    pytest.param(STORY + "🙂📌", STORY + "✨🔥", True,
                 id="decorative-emoji-only"),
    pytest.param("该项目今天公布调查结果。" * 6 + "后续将公布证据。" * 3,
                 "后续将公布证据。" * 3 + "该项目今天公布调查结果。" * 6,
                 True, id="same-paragraphs-reordered"),
    pytest.param(ENGLISH, ENGLISH.upper() + "   Source: https://t.me/updates",
                 True, id="english-case-and-attribution"),
    pytest.param("ＡＢＣ公司今日公布业绩，累计利润增长１０％。" * 3,
                 "ABC公司今日公布业绩 累计利润增长10%。" * 3,
                 True, id="unicode-width-and-punctuation"),
    pytest.param(STORY, STORY.replace("时间线", "时\u200b间线"), True,
                 id="zero-width-formatting"),
    pytest.param(STORY + "\n频道 https://t.me/alpha",
                 STORY + "\n频道 https://t.me/beta", True,
                 id="different-channel-links"),
    pytest.param(STORY[:95], STORY[:95] + "\n" + "不同的新分析和补充材料。" * 20,
                 False, id="quote-with-substantial-new-commentary"),
    pytest.param("【频道公告】以下是每个频道都会重复使用的开头内容。" + STORY,
                 "【频道公告】以下是每个频道都会重复使用的开头内容。" + ENGLISH,
                 False, id="shared-header-different-story"),
    pytest.param(STORY + "获批。", STORY + "遭拒。", False,
                 id="contradictory-final-conclusion"),
    pytest.param("节点出现硬盘故障，机房已安排更换新硬盘，数据无法保留，恢复后会重装系统。"
                 "正在依次重装中。",
                 "节点出现硬盘故障，机房已安排更换新硬盘，数据无法保留，恢复后会重装系统。"
                 "已完成重装系统，请自行开机使用。",
                 False, id="realistic-outage-in-progress-vs-restored"),
    pytest.param(STORY + "预计不会调整。", STORY + "预计会调整。", False,
                 id="changed-chinese-negation"),
    pytest.param(ENGLISH + " It will not proceed.", ENGLISH + " It will proceed.",
                 False, id="changed-english-negation"),
    pytest.param(STORY + "成本100万元收入200万元。",
                 STORY + "成本200万元收入100万元。", False,
                 id="swapped-figure-labels"),
    pytest.param(STORY + "上涨+10%。", STORY + "上涨-10%。", False,
                 id="changed-numeric-sign"),
    pytest.param(STORY + "收入1,000元。", STORY + "收入1.000元。", False,
                 id="decimal-and-thousands-punctuation"),
    pytest.param(STORY + "预计2026年完成。", STORY + "预计2027年完成。", False,
                 id="changed-year"),
    pytest.param(STORY + "项目最终获批。",
                 STORY + "项目最终获批。最新调查已撤销这一决定。",
                 False, id="one-sided-substantive-update-is-not-channel-footer"),
    pytest.param(STORY.replace("确认讨论的是同一件事", "确认讨论的是同一件事，预计一年完成"),
                 STORY.replace("确认讨论的是同一件事", "确认讨论的是同一件事，预计两年完成"),
                 False, id="changed-chinese-numeral-in-core"),
    pytest.param(STORY.replace("希望大家阅读", "强烈反对大家阅读"),
                 STORY.replace("希望大家阅读", "支持大家阅读"),
                 False, id="changed-assessment-in-core"),
    pytest.param(ENGLISH + " The meeting is on Tuesday.",
                 ENGLISH + " The meeting is on Wednesday.",
                 False, id="changed-english-weekday"),
    pytest.param("下一次会议在周一召开，大家提前做好准备，会上会讨论项目预算和进度。",
                 "下一次会议在周一召开，大家提前做好准备，会上会讨论项目预算和进度。", True,
                 id="sufficient-length-exact-short-story"),
    pytest.param("会议今天下午举行。", "会议今天下午举行。", False,
                 id="very-short-generic-exact-not-suppressed"),
    pytest.param("下周召开发布会讨论新项目进展和大家关心的问题。",
                 "下周召开发布会讨论旧项目进展和大家关心的问题。",
                 False, id="short-fuzzy-is-unsafe"),
])
def test_repost_or_distinct_story(first, second, duplicate):
    a, b = canonicalize(first), canonicalize(second)
    assert is_duplicate(a, b) is duplicate
    assert is_duplicate(b, a) is duplicate  # source arrival order must not matter


def test_normalizer_distinguishes_english_word_boundaries_and_financial_signs():
    assert canonicalize("ABC DEF") != canonicalize("ABCDEF")
    assert canonicalize("收益 +10%") != canonicalize("收益 −10%")
    assert canonicalize("收入 1,000") != canonicalize("收入 1.000")
