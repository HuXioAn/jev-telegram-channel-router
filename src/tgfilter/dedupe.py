"""Conservative, CPU-only comparison of recently delivered public posts.

The source URL and channel name are intentionally excluded: they differ when
one author submits the same body to multiple channels. No model, language
segmenter or probabilistic hash is needed for a one-day per-chat window.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

_NUMBERS = re.compile(r"[+\-]?\d+(?:[.,]\d+)?")
_EN_NEGATIONS = re.compile(r"(?<![a-z])(?:not|never|without|no)(?![a-z])")
_ZH_NEGATIONS = "不没無无未非否"
_EXACT_MIN = 24
_FUZZY_MIN = 60
_SHINGLE = 4


def canonicalize(text: str) -> str:
    """Normalize typography while preserving meaningful numbers and links.

    Do not strip arbitrary first/last lines: channel-specific promotional tails
    are tolerated by the similarity check. A URL or an English word boundary
    may distinguish two otherwise similar news items; deleting them wholesale
    would create false positives.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    # Nearest significant character to the right of each position. Construct
    # once so punctuation-heavy texts remain O(length), not quadratic.
    following: list[str] = [""] * len(text)
    right = ""
    for i in range(len(text) - 1, -1, -1):
        following[i] = right
        if text[i].isalnum():
            right = text[i]
    chars: list[str] = []
    left = ""
    for i, ch in enumerate(text):
        if unicodedata.category(ch)[0] in ("L", "N"):
            chars.append(ch)
            left = ch
            continue
        # Signs, commas and decimal points can change a financial claim.
        if ch in "+-−" and i + 1 < len(text) and text[i+1].isdecimal():
            chars.append("-" if ch == "−" else ch)
            continue
        if ch in ".," and i and i + 1 < len(text) and text[i-1].isdecimal() and text[i+1].isdecimal():
            chars.append(ch)
            continue
        # Ignore separators in Chinese, but do not collapse "a b" into "ab".
        if left.isascii() and left.isalnum() and following[i].isascii() and following[i].isalnum():
            if chars and chars[-1] != " ":
                chars.append(" ")
    return "".join(chars).strip()


def _shingles(text: str) -> Counter[str]:
    # A multiset, not a set: repeating the same phrase must count as content,
    # otherwise a short channel footer can swamp a long repeated body.
    return Counter(text[i:i + _SHINGLE] for i in range(len(text) - _SHINGLE + 1))


def _negations(text: str) -> Counter[str]:
    return Counter(ch for ch in text if ch in _ZH_NEGATIONS) + Counter(
        _EN_NEGATIONS.findall(text))


def _ordered_subsequence(shorter: list[str], longer: list[str]) -> bool:
    iterator = iter(longer)
    return all(any(value == item for value in iterator) for item in shorter)


def _changed_short_ending(a: str, b: str) -> bool:
    """Veto a tiny two-sided replacement after a long shared narrative.

    One-sided additions (a channel footer) are fine. Two conflicting final
    claims such as "获批" versus "遭拒" must not be hidden just because the
    preceding report is identical. This deliberately favors a false negative
    on two *different* very short footers over losing a real update.
    """
    common = 0
    for left, right in zip(a, b):
        if left != right:
            break
        common += 1
    shorter = min(len(a), len(b))
    return (common >= shorter * 0.8
            and 0 < len(a) - common <= max(12, shorter // 10)
            and 0 < len(b) - common <= max(12, shorter // 10))


def is_duplicate(candidate: str, previous: str) -> bool:
    """Compare canonicalized text; avoid suppressing short/generic posts.

    The Jaccard branch handles small changes or paragraph rearrangements. The
    containment branch handles a copied core plus a modest source-specific
    footer. A length-ratio guard prevents a short quoted excerpt from hiding a
    substantially different longer post.
    """
    shorter, longer = sorted((len(candidate), len(previous)))
    if shorter < _EXACT_MIN:
        return False
    if candidate == previous:
        return True
    if shorter < _FUZZY_MIN or shorter / longer < 0.65:
        return False
    if _changed_short_ending(candidate, previous):
        return False
    a, b = _shingles(candidate), _shingles(previous)
    if not a or not b:
        return False
    common = (a & b).total()
    jaccard = common / ((a | b).total())
    containment = common / min(a.total(), b.total())
    # Financial and news templates often differ by just a critical figure;
    # never hide such a post on fuzzy resemblance alone. New numbers in a
    # longer channel footer do not invalidate an otherwise identical core.
    candidate_numbers = _NUMBERS.findall(candidate)
    previous_numbers = _NUMBERS.findall(previous)
    if not (_ordered_subsequence(candidate_numbers, previous_numbers)
            or _ordered_subsequence(previous_numbers, candidate_numbers)):
        return False
    # A one-word inversion can leave almost all shingles unchanged. Prefer
    # delivering a possible duplicate to suppressing a changed claim.
    if _negations(candidate) != _negations(previous):
        return False
    return jaccard >= 0.78 or containment >= 0.88
