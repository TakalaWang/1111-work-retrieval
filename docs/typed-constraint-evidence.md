# Typed query-constraint evidence and promotion boundary

This report pins the production grammar to observed source fields instead of treating arbitrary
keywords as hard filters. The snapshot is reproducible from:

- `dataset/職缺.csv`: 1,218,635 rows, SHA-256
  `53937f7bf076789c4cd7e3be34fb89875336108d57707b5a93182181e1087089`.
- `dataset/userSearchLog_20260601_20260607.csv`: 6,154,166 rows, SHA-256
  `10a84a79730f6593ddfcddf259ddb2020e10618dc7f0ea6f5566e73f464c6d1d`.

The counts below use non-empty `ks` values and Unicode-aware regular expressions. They are evidence
for grammar coverage, not relevance labels and not an evaluation set.

| Constraint    | Typed JD source |                                        Observed source shape |     Explicit query rows | Production grammar                                                        |
| ------------- | --------------- | -----------------------------------------------------------: | ----------------------: | ------------------------------------------------------------------------- |
| Job attribute | `職缺屬性`      |   0 missing; `全職` 1,031,266, `兼職` 133,514, `工讀` 51,196 |                 305,484 | one unambiguous cue: `正職/全職`, `兼職`, or `工讀`                       |
| Work shift    | `工時`          |                              5,521 missing; 112 combinations |                  81,040 | exactly one of `日班/中班/晚班/假日班/輪班`                               |
| No experience | `工作經驗需求`  | 0 missing; 20 values; `不拘` 858,325 and `無工作經驗` 14,248 | 20,369 for no/year cues | only `無經驗/無工作經驗`; matches `不拘` or `無工作經驗`                  |
| Management    | `管理人數`      |    0 missing; 7 values; 84,502 explicitly require management |                     694 | responsibility cues `管理人數/帶領團隊/帶人`; bare `管理職` stays lexical |
| Travel        | `是否需外派`    |                                          0 missing; 4 values |                      19 | not promoted: willingness and employer requirement are not equivalent     |

Numeric experience such as `三年工作經驗` is also not promoted. Bare `管理職` is job-title intent and
cannot safely imply that the organizer's `管理人數` field must be positive, so it remains lexical. The query can
describe the candidate's experience or a desired JD threshold, while the source field is an employer
minimum; there is no typed request field that disambiguates those intents. Multiple conflicting
job-attribute or shift cues likewise compile to no hard constraint.

Negative phrases use one local-negation policy across education, job attribute, work shift,
no-experience, and management cues. Examples such as `不要兼職`, `非工讀`, `不找晚班`, `非管理職`,
`不接受無經驗`, `不輪班`, `無需輪班`, and `不需帶人` fail closed to no positive constraint;
negative filtering itself is not claimed by the current schema.

Every promoted constraint is immutable in `CandidateRequest`, applied by Tantivy before each lane's
Top-K (including the exact-dense eligible universe), and revalidated against PostgreSQL metadata.
Any incumbent mismatch fails closed and is reported as index/database drift in the audit trace.

The pinned JD snapshot also contains 82 rows whose salary lower or upper bound is fractional and
therefore cannot satisfy the integral salary contract. The builder keeps those jobs available to
unconstrained retrieval, omits both salary filter fields for the affected row, and records the exact
exclusion count in `build-manifest.json`. PostgreSQL applies the same pair-wise quarantine, so an
invalid upper bound cannot accidentally fall back to a valid lower bound.

The update timestamp is a snapshot field, not a creation timestamp. In the formal Top-10 audit,
1,431 of 2,908 returned snapshots are later than the June 8 Demo as-of; among positive context-job
pairs, 11,082 of 13,896 (79.75%) have a snapshot update later than the query day. Consequently,
production applies only `updated_at >= as_of - 180 days` before Top-K. It retains later snapshots,
assigns them freshness `0`, and emits `future_updated_snapshot=true`; no upper-bound eligibility
claim is made.

## Information-completeness prior: design only

The ten-field candidate definition is `職務名稱`, `職務內容`, `薪資`, `職務小類`, `工作城市`,
`職缺屬性`, `工時`, `學歷需求`, `工作經驗需求`, and `產業小類`. Coverage is the unweighted
fraction of those fields that are neither blank nor `NULL`. On the pinned JD snapshot, 1,199,221
jobs have all 10 fields; 18,914 have 9; 494 have 8; 5 have 7; and 1 has 6. Individual coverage is
98.891% for description, 99.547% for work shift, at least 99.941% for every other field.

This feature is **not in runtime** because no fixed evaluator or relevance-band calibration contract
has approved it. A candidate implementation may only order jobs inside an exact semantic-score tie
(before the final `job_id` tie-break), must emit the ten presence bits and aggregate score in trace,
and must pass all promotion gates:

1. fixed train/validation inputs and organizer metric script; no test-set tuning;
2. no NDCG@10, MRR, Precision@10, or Top-1 regression;
3. no pair outside an exact semantic-score tie changes order;
4. top-10 missing-field rate improves with bootstrap confidence reported;
5. the feature can be disabled by one manifest flag and its ablation is reproduced by one script.

Until these gates pass, completeness remains experiment evidence rather than a serving claim.
