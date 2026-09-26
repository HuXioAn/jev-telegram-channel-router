"""Low-CPU, language-independent near-duplicate checks."""

from tgfilter.dedupe import canonicalize, is_duplicate


def match(a: str, b: str) -> bool:
    return is_duplicate(canonicalize(a), canonicalize(b))


def test_exact_normalized_text_ignores_punctuation_spaces_and_width():
    assert match("突发！ＡＢＣ公司的年度报告：盈利123亿元，预计明年继续增长。",
                 "突发 ABC 公司的年度报告 盈利１２３亿元 预计明年继续增长")
    assert not match("新项目 123", "新项目 124")  # short generic content is unsafe


def test_same_long_story_with_different_channel_footers_and_urls():
    body = "今天去医院复查，医生说各项指标恢复正常，我想把这个好消息分享给一直关心我的朋友们。" * 3
    a = body + "\n\n📮投稿 ☘️频道 🐧聊天 https://t.me/first"
    b = "【网友投稿】" + body + "\n关注本频道 https://t.me/second"
    assert match(a, b)


def test_minor_edits_in_chinese_and_english_remain_near_duplicates():
    chinese = "这个项目已经公布正式的申请时间，周一开始提交资料，周五截止，请大家提前准备。" * 3
    assert match(chinese, chinese.replace("周一", "星期一", 1))
    english = ("The company published its final report on Tuesday, and the results "
               "were reviewed by the independent auditors before publication. ") * 2
    assert match(english, english.replace("Tuesday", "Wednesday", 1) + " Follow our channel!")


def test_shared_generic_lead_but_different_main_content_is_not_duplicate():
    intro = "【粉丝投稿】大家好，我想分享一下最近发生的一件事情："
    one = intro + "朋友组织去爬山，大家提前做好准备，路线很顺利，回家后一起吃饭庆祝。" * 3
    two = intro + "公司要求调整工作安排，大家讨论了新方案，时间有所变化，会议下周继续。" * 3
    assert not match(one, two)


def test_long_distinct_commentary_is_not_suppressed_just_because_it_quotes_a_post():
    original = "这是一个详细的事情经过，包含时间地点人物和调查过程，希望后续能够得到官方回应。" * 2
    commentary = original + "完全不同的追加分析和评论，以及需要单独报道的背景材料。" * 12
    assert not match(original, commentary)


def test_short_near_duplicates_default_to_not_suppressing():
    assert not match("同一天上午开会讨论计划。", "同一天上午开会讨论方案。")
    assert not match("快来看 https://t.me/channel/1", "快来看 https://t.me/other/2")


def test_canonicalization_keeps_non_latin_letters_and_numbers():
    assert canonicalize(" 你 好、ПрИвЕт！ＡＢＣ １２３ ") == "你好приветabc 123"


def test_changed_financial_figures_and_negation_are_not_suppressed():
    prefix = "公司公布了年度业绩报告，管理层解释本季度的数据和后续计划，投资者据此讨论项目价值。" * 3
    assert not match(prefix + "最终利润为 10.5 亿元。", prefix + "最终利润为 15 亿元。")
    assert not match(prefix + "预计不会继续上涨。", prefix + "预计会继续上涨。")
    en = ("The regulator published a detailed statement about the request and "
          "outlined the evidence considered by its independent review panel. ") * 2
    assert not match(en + "It will not be approved.", en + "It will be approved.")


def test_numeric_sign_decimal_notation_and_number_association_are_material():
    intro = "这是本季度公司的业绩说明，管理层向投资者展示收入、成本与净利润的实际变化。" * 3
    assert not match(intro + "净利润 +10%。", intro + "净利润 -10%。")
    assert not match(intro + "净利润 1,000 元。", intro + "净利润 1.000 元。")
    assert not match(intro + "收入100万元，成本200万元。",
                     intro + "收入200万元，成本100万元。")


def test_changed_conclusion_is_not_a_reposted_footer():
    intro = "市政府连续多日对这个项目展开了公开讨论，相关部门已经提交材料并等待正式结果。" * 3
    assert not match(intro + "申请最终获批。", intro + "申请最终遭拒。")
