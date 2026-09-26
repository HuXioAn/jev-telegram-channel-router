# Recent-post deduplication

Deduplication runs **after** a Jev match and **before** each real destination
send. A match is suppressed only if a similar post was successfully sent to
**that same chat** in the last 24 hours. One chat's history never prevents a
different chat from receiving its first copy. `/test` samples neither consult
nor populate this history. The source-channel judging cache, quotas, and
consumption cursors remain separate; a duplicate still counts as a matched
and consumed post, but not as a delivery.

## Why this algorithm

A 24-hour per-chat history is small, so an exact comparison against the few
recent candidates is simpler and more accurate than a probabilistic SimHash or
MinHash/LSH index. No model, segmentation dictionary, network request, or new
dependency is involved.

1. Use the fetched post body, **not** the formatted Telegram push or its
   channel/source link. Jev and the push still use the first 500 characters;
   deduplication may inspect up to 4,000 source characters to distinguish long
   stories with the same opening. When the source body exceeds this bound, it
   is **not suppressed or recorded**: an unseen ending could change its meaning.
2. Unicode NFKC and case-fold; simplify punctuation and Chinese spacing while
   retaining English word boundaries, numbers (including decimal points), and
   meaningful URLs. Do not delete entire first/last lines or use brittle
   channel-specific boilerplate rules. Different channel links in a modest
   footer can be tolerated by the comparison without erasing main-body links.
3. Exact normalized text (at least 24 characters) is a duplicate. For texts
   at least 60 characters long with a length ratio of at least 0.65, compare
   **multisets of overlapping four-character shingles**. Suppress if weighted
   Jaccard is at least 0.75, or if at least 0.86 of the shorter post's shingles
   occur in the longer one. The asymmetric score catches a common body with a
   moderate channel-specific footer; the length guard avoids equating a short
   quotation with an otherwise new article. A multiset correctly counts
   repeated words/phrases. Numeric signs and comma/decimal notation are kept;
   the **ordered** sequence of figures must be compatible (allowing an extra
   figure in one footer). Changed negations and conflicting substantive endings
   after an otherwise identical body also block fuzzy suppression. These
   conservative guards can miss some true reposts, but avoid hiding material
   changes. Very short/generic posts are only suppressed on a reasonably long
   exact match, not a fuzzy guess.
   For otherwise strong fuzzy candidates, a bounded text alignment rejects
   two-sided replacements inside the core; different channel headers/footers
   and paragraph movements are still allowed. Chinese weekday synonyms such
   as `周一` and `星期一` are normalized before alignment.
   A one-sided addition is treated as a footer only when it resembles channel
   attribution or a `t.me` link; added substantive prose is sent.
4. An `asyncio.Lock` per destination spans check → Telegram send → SQLite
   record, preventing simultaneous channel rounds from both sending the same
   text to one chat. Save only after that specific send succeeds. `delivered_posts`
   has an index on `(chat_id, sent_at)` and old rows are deleted by the normal
   one-minute scheduler tick. On a send failure, no deduplication row is added.

The scores and length guards are conservative, **not universal semantic
similarity**. Paraphrases/translations, image-only posts, texts over 4,000
characters, and different-looking OCR text are not reliably deduplicated.
Conversely, two genuinely distinct posts with almost the same body can still
be mistaken for copies; changing a conclusion away from the ending is not
always detectable without semantic understanding. If a Telegram send succeeds
but its acknowledgement is lost, perfect exactly-once delivery is impossible without
Telegram-side idempotency; unconfirmed sends are not recorded in the
24-hour duplicate history. Existing subscription-cursor behavior on delivery
failures is unchanged by this feature.

## Validation and cost

The public example posts [WantAnswer/7377](https://t.me/WantAnswer/7377) and
[chxpd/3553](https://t.me/chxpd/3553) differ by a channel-specific footer;
they remain recognized as duplicates on a fresh read-only preview fetch.

A larger **read-only retrospective** test used an existing local archive of
public Telegram previews (posts dated 2022-02 through 2026-09; not distributed
with this repository). Of 15,573 archived posts, 12,112 had usable, nonempty
body text within the comparison length limit, from 268 source channels. The
24-hour posting-time window yielded 1,527,108 pairs; 813,680 had at least
24 canonical characters on both sides and were actually compared. In 22.7 s
of local CPU/wall time, 771 pairs were flagged: 485 raw-text exact, 259
normalized-text exact, and 27 fuzzy. Among 121 pairs with matching archived
forward-origin identifiers, 111 were flagged and 10 left to send; the latter
include genuine editorial additions. Forward-origin identity is only a
**proxy** for related posts, not ground truth for whether their full texts
should be suppressed.

In a separate stress simulation that assumes *every* usable post was Jev-matched,
sent successfully to **one shared destination**, and sent at its posting time,
11,544 posts would be sent and 568 suppressed. Suppressed posts were **not**
placed into the comparison history. This deliberately unrealistic all-to-one
simulation does not predict traffic to any actual subscriber. The 771 pair
matches likewise are not a count of real-world suppressed deliveries. Manual
inspection of the 27 fuzzy flagged pairs identified one status-update risk in
the previous version: an outage notice saying “reinstallation in progress”
versus “restored.” A regression test and a stricter two-sided-ending veto now
send both updates; an unchanged repeat of the restored notice still matches.
Text-only comparisons cannot tell whether equal captions accompany different
photos. No fully labeled corpus or production end-to-end send test exists, so
the above is **not a measured precision/recall guarantee**.

On the evaluation host, 1,000 comparisons of synthetic ~460-character texts
took 254 ms (unrelated) or 284 ms (repost with footer); 100 comparisons of
synthetic ~3,400-character texts took 176 ms or 196 ms respectively. These
wall-clock microbenchmarks exclude canonicalization, database reads, and
network; machine and input dependent. The live
instance previously recorded 26 deliveries in a 24-hour period across its
existing destinations, so indexing/LSH would be unnecessary complexity.

Shingling with resemblance/containment follows [Broder, *On the Resemblance
and Containment of Documents* (1997)](https://cadmo.ethz.ch/education/lectures/FS18/SDBS/papers/broder.pdf).
MinHash/LSH, described in [*Mining of Massive Datasets*, ch. 3](http://infolab.stanford.edu/~ullman/mmds/ch3n.pdf),
helps when candidate sets are massive; it is not needed for this bounded window.
