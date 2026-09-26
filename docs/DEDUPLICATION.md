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
   figure in one footer). Changed negations and two conflicting short endings
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

The public example posts `WantAnswer/7377` and `chxpd/3553` differ by a
channel-specific footer; both are recognized as duplicates. In one read-only
check of public windows from these and two English-language channels, one
duplicate was flagged among 226 post pairs published within 24 hours, from 68
fetched posts. The highest other weighted Jaccard score was 0.204. This is a
sanity check, not a measured precision/recall guarantee.
On the evaluation host, 1,000 comparisons of approximately 500-character texts
took 203 ms wall time; 100 comparisons of approximately 4,000-character texts
took 146 ms (one process, no network; machine dependent). The live
instance previously recorded 26 deliveries in a 24-hour period across its
existing destinations, so indexing/LSH would be unnecessary complexity.

Shingling with resemblance/containment follows [Broder, *On the Resemblance
and Containment of Documents* (1997)](https://cadmo.ethz.ch/education/lectures/FS18/SDBS/papers/broder.pdf).
MinHash/LSH, described in [*Mining of Massive Datasets*, ch. 3](http://infolab.stanford.edu/~ullman/mmds/ch3n.pdf),
helps when candidate sets are massive; it is not needed for this bounded window.
