# Agentic Weekly Status Pipeline Audit

**Audit date:** 2026-09-22
**Reviewed revision:** `93593c7` (`test/upstream-main-workflow`, matching `upstream/main`)
**Purpose:** Living record of audit findings, problems, feature requests, and enhancement ideas. Update this document as findings are resolved or new work is proposed.

## Executive assessment

The workflow is sound: collect Jira and GitHub evidence, create a per-person draft, require human review in Slack, preserve confirmed revisions in a ledger, and synthesize a management report. Moving people from authoring to reviewing should reduce weekly friction while the human-confirmation gate limits unsupported AI claims.

The project is a late-stage prototype rather than production-ready automation. The component features are largely present, and all 97 unit tests pass, but the newly merged orchestration has defects that prevent the scheduled workflow from operating as documented. The prompt skills are well designed in principle but contain contract mismatches and ambiguous rules that can lead to inconsistent or unverifiable output.

## Current project status

- M0-M5 capabilities are largely implemented: collection, drafting, Slack confirmation/edit/regeneration, ledger persistence, synthesis, and delivery.
- M6 CronJob automation is merged into `upstream/main`.
- Unit tests: **97 passed**.
- Ruff: **78 findings**, including undefined names that represent runtime failures.
- Strict mypy: **56 errors across 15 files**.
- No repository CI workflow was found.
- The working tree contains local/generated artifacts and a modified Postgres deployment document.
- A critical M6 repair exists on unmerged branch `fix/m6-lock-and-report-synthesize` (`d01b85f`).

## Priority 0: release blockers

### P0.1 Monday reporting crashes

`src/status/cli.py` calls `run_synthesizer` from `batch_lock_and_report`, but the function is not imported. The job expires unconfirmed participation and then raises `NameError` before report generation.

**Recommendation:** Merge or recreate `d01b85f`, route the job through `synthesize_report`, persist the report audit chain, and retain its added tests.

### P0.2 All CronJobs use incorrect local hours

The manifests specify `timeZone: America/New_York`; therefore cron expressions are already interpreted in Eastern time. The expressions currently contain UTC-converted hours:

| Job | Intended | Current effective time | Correct expression |
|---|---:|---:|---|
| collect-and-draft | Fri 08:30 ET | Fri 13:30 ET | `30 8 * * 5` |
| send-drafts | Fri 09:00 ET | Fri 14:00 ET | `0 9 * * 5` |
| nudge | Fri 14:00 ET | Fri 19:00 ET | `0 14 * * 5` |
| lock-and-report | Mon 09:00 ET | Mon 14:00 ET | `0 9 * * 1` |

### P0.3 Reminder automation uses an undefined logger

`batch_nudge` references `log` on its skip, success, and exception paths, but `src/status/cli.py` never defines it. The reminder job can fail before or after sending a message.

### P0.4 Batch failures return success to Kubernetes

The collect/send commands print person-level failures but do not return a nonzero exit code. Kubernetes will mark partially or wholly failed jobs successful, suppressing retries and failure alerts.

### P0.5 Per-person failure isolation is unsafe

`run_batch_operation` reuses one SQLAlchemy session across all people and does not roll it back after an operation fails. A database exception can leave the session unusable, causing later people to fail. It also classifies collection and drafting failures as `send_failed`.

**Recommendation:** Use one transaction/session per person, explicitly roll back failures, and record stage-specific states such as `collect_failed`, `draft_failed`, and `send_failed`.

## Priority 1: production hardening

- The merged Monday workflow bypasses complete `report_run`/`report_entry` persistence and delivery metadata. Preserve the promised audit chain.
- No tests cover the merged batch commands, schedule semantics, partial failure behavior, or job exit codes.
- CronJobs and the Deployment use mutable `:latest` images. Use version tags and preferably immutable digests.
- Add job deadlines, sensible retry policy, failure alerting, and structured run-level logging.
- Add Slack bot readiness/liveness signals or an external Socket Mode health check.
- Do not expose Postgres through a public LoadBalancer by default. Prefer private networking, VPN/bastion access, or tightly restricted source ranges and TLS.
- Keep `.env`, credentials, kubeconfigs, rendered reports, secrets, and generated artifacts outside the repository workspace where practical. Add secret scanning to CI.
- Explicitly authorize the acting Slack user in the confirm handler, matching edit and regenerate authorization.
- Constrain participation states at the model/database layer instead of accepting arbitrary strings.
- Make cutoff, expiration, synthesis, persistence, and delivery a recoverable state machine. A synthesis failure should not leave an opaque half-completed run.
- Add a defined policy for a late confirmation, regeneration, or correction after the report cutoff.

## Skill audit

### Overall skill assessment

Both skills have the right safety posture: evidence-first drafting, no invented impact, human confirmation, separation of drafting from synthesis, and traceability into the final report. The drafter is especially strong about avoiding inflated language and duplicate work. The synthesizer preserves most legacy categorization and formatting requirements.

The main opportunity is to make the written skill contracts exactly match the Pydantic schemas, collector output, post-processing behavior, and measurable acceptance criteria.

### Drafter skill findings

#### D1. `quiet` conflicts with mandatory evidence

The skill allows a `quiet` entry for an epic with no activity, but `DraftEntry.evidence` has `min_length=1`. A genuinely quiet entry has no current-week evidence, so the model must either fail validation, cite stale evidence, or violate the definition of quiet.

**Recommendation:** Prefer omitting quiet entries and representing stale/silent epics as flags. Alternatively, make evidence optional only for `quiet` and enforce the conditional rule in Pydantic.

#### D2. Empty or partial payload behavior is underspecified

The skill says an empty payload should return no entries, but does not distinguish:

- both collectors returning no activity successfully;
- one collector failing while the other succeeds;
- required identity/date fields missing;
- collector truncation caused by configured result limits.

**Recommendation:** Add `collection_errors`, collector status, and truncation metadata to the formal input contract. Allow valid partial drafts, flag the unavailable source, and reserve an empty result for missing identity/date or no usable evidence.

#### D3. Input documentation does not fully match actual normalized Jira data

The example mentions `assignee_account_id`, while the current collector uses email-based selection and emits `assignee_display_name`, `reporter_display_name`, `is_assignee`, and `is_reporter`. The `person` description still says “Jira account id or handle.”

**Recommendation:** Document `person` as the stable ledger person ID and copy the actual collector payload fields into the contract from one shared schema/example.

#### D4. Ownership rules need a deterministic activity signal

The skill says to use transitions and comments as evidence that the person worked on an issue. The collector filters comments by the person's email, but transitions do not identify who performed them in the normalized payload. A workflow transition on someone else's issue can therefore be attributed incorrectly.

**Recommendation:** Include `actor_is_person` or actor identity for transitions, and state a strict precedence rule for ownership. Do not ask the model to infer actor ownership from an actorless transition.

#### D5. State classification is not operationally testable

Terms such as “normal pace,” “substantially longer,” and “expected movement” lack thresholds. Three weeks of prior entries are not necessarily enough to infer comparable ticket duration or closure rates.

**Recommendation:** Calculate objective features in code—days in progress, explicit blocked state, transitions this week, merged PR count, and silent-week count—and give the skill deterministic classification rules. Treat “slipped” as human-review-required unless explicit due-date or historical evidence exists.

#### D6. “Epic accumulating tickets faster than it closes them” is unsupported

The payload contains only person-scoped weekly issues, not a complete epic backlog history. The skill cannot reliably calculate intake versus closure rate.

**Recommendation:** Remove this flag until the collector supplies complete epic-level counts over a defined period.

#### D7. Tailored unticketed prompts may require unavailable context

The example references a Tuesday partner sync, but the payload has no calendar, Slack, or meeting data. The model must not imply knowledge it was not given.

**Recommendation:** Require prompts to reference only observed signals, such as an unlinked PR or commit. Otherwise use a neutral prompt and optionally add calendar/Slack evidence through an explicitly governed collector later.

#### D8. Hyperlink ownership is split between the model and code

The skill requires Markdown links in `outcome`, while post-processing also injects links and constructs labels. This creates duplicated responsibility and possible malformed or nested links.

**Recommendation:** Choose one boundary. Prefer structured plain-text outcomes plus evidence labels from the skill, then render links deterministically in code. If the model retains link generation, validation must verify every URL and reject evidence/outcome mismatches.

#### D9. Evidence is entry-level rather than claim-level

An entry can contain multiple clauses and multiple evidence items without declaring which evidence supports each clause.

**Recommendation:** For stronger auditability, represent outcomes as one or more `{text, evidence_refs}` claims and render them into prose after validation.

#### D10. Output invariants are described but not schema-enforced

Examples include requirements such as low confidence implying `needs_human`, an unepic'd entry requiring human review, and `why_flagged` accompanying review. Pydantic currently accepts violations.

**Recommendation:** Add model validators for these invariants and verify that every evidence item exists in the input payload.

#### D11. Prompt version is not immutable

Rows record a string based on `latest`, which does not identify the actual instructions that generated a draft.

**Recommendation:** Record provider skill version/ID plus a hash of the local skill content and invocation template.

### Synthesizer skill findings

#### S1. The documented input omits `flags`

`SynthesisInput` contains `flags`, but the skill contract only documents entries and participation. Current construction always passes `flags=[]`, so the schema and prompt suggest a capability the pipeline does not meaningfully use.

**Recommendation:** Either remove flags from synthesis completely, consistent with “never include gap commentary,” or document them as non-reportable validation context with explicit behavior.

#### S2. Hyperlink rules conflict

The skill says both “every hyperlink from source evidence must appear” and “do not invent URLs not present in evidence,” while also instructing the model to build Jira URLs from bare keys. A bare Jira key is not itself a URL.

**Recommendation:** Normalize all evidence to `{type, id, url, label}` before invocation. Then require the report to use only provided URLs. Define whether every evidence URL is mandatory or only those supporting included claims.

#### S3. Mandatory inclusion of every evidence URL can reduce readability

A rolled-up epic may contain many commits, tickets, and PRs. Requiring all links can create a management report that is longer and less useful than the confirmed status itself.

**Recommendation:** Require at least one supporting link per distinct claim and preserve all source URLs in the ledger/audit view, not necessarily in the management prose. If compliance requires all links, state that explicitly as a business requirement and test it.

#### S4. Categorization relies on fragile keywords

Rules such as partner work versus work on a certification tool can be ambiguous from prose alone. The `project` field is usually a Jira project key and is not a reliable reporting category.

**Recommendation:** Add structured `report_category` and `report_group` fields to confirmed entries, editable during Slack review. Let the skill suggest them, but make code or human confirmation authoritative.

#### S5. Rollup can erase individual attribution and distort grammar

Combining multiple people's outcomes under one epic with semicolons can imply a single actor or chronology. The report intentionally organizes by initiative, but the rule needs a defined treatment for distinct or conflicting updates.

**Recommendation:** Combine only compatible clauses. Preserve separate bullets when outcomes represent different workstreams, blockers, or asks, even if the epic key matches.

#### S6. Non-responder placement is ambiguous

The skill allows a note at the end of a “relevant section” or a Team participation note, but a non-responder has no reliable relevant section when they supplied no entry.

**Recommendation:** Always use a single `## Team participation` section for expired or failed participation, with separate wording for `expired`, `send_failed`, and `on_leave`. Do not label `sent` as a non-responder before cutoff.

#### S7. Participation semantics are incomplete

The skill only explicitly handles `expired`, although the contract includes `on_leave`, `sent`, and `send_failed`.

**Recommendation:** Define exact report behavior for every enum value and distinguish “person did not respond” from “system failed to contact person.”

#### S8. `entries_cited` identifiers are not unique

`person_id:epic_key` collapses multiple unticketed entries for the same person and can also be ambiguous across revisions. The ledger already has UUID entry IDs, but they are not passed to synthesis.

**Recommendation:** Include immutable `entry_id` in `SynthesisEntry` and return those exact IDs in `entries_cited`. Validate citations against the input before persistence.

#### S9. Output completeness is not fully validated

The code sanitizes Markdown, but the schema does not prove that every confirmed entry was represented, every ask appeared, every non-responder was included, sections match content, or citations are valid.

**Recommendation:** Add deterministic post-generation validators. Fail or retry synthesis when asks, required participation notices, or cited entries are missing or fabricated.

#### S10. Legacy rules contain subjective wording

“Sensible subsection name,” “light clarity edits,” and “partner-facing updates” can yield inconsistent categorization and rewriting.

**Recommendation:** Provide a compact decision table with precedence, negative examples, and an `Uncategorized` fallback that triggers human review rather than confident guessing.

#### S11. Date and Markdown production should be deterministic

The model does not need to format the heading, sort bullets, normalize semicolon capitalization, or serialize the final template. These are mechanical tasks and can drift.

**Recommendation:** Have the skill return structured categorized groups and claims. Render final Markdown in code, where date format, order, headings, links, and punctuation can be tested exactly.

## Recommended skill architecture

1. Define shared Pydantic/JSON Schemas for collector input, draft output, confirmed ledger input, and synthesis output.
2. Generate skill contract examples from those schemas or test checked-in examples against them.
3. Keep the LLM responsible for semantic work: grouping, conservative summarization, suggested state/category, and identifying explicit blockers/asks.
4. Keep code responsible for identity, ownership filters, dates, URLs, sorting, formatting, citations, deduplication, and completeness validation.
5. Store immutable prompt/skill hashes and model/provider invocation metadata.
6. Build a small golden evaluation set containing sparse weeks, partial collector failure, unlinked work, actor ambiguity, multiple people on one epic, blockers, asks, PTO, and non-response.
7. Score factual support, omission, categorization, edit distance after human review, link correctness, and unsupported-claim rate.

## Product and workflow enhancements

### User experience

- Allow users to add missing work, blockers, leadership asks, and a new initiative—not only edit generated outcomes.
- Add explicit “no meaningful update,” PTO, and leave flows.
- Display evidence beside each draft claim.
- Consider per-entry confirmation when one item is questionable.
- Show the deadline, lock state, and confirmation state in Slack.
- Persist regeneration progress/failure instead of relying only on an in-process daemon thread.
- Add a manager/operations view for confirmed, pending, failed, expired, and on-leave participation.

### Quality measurement

- Measure draft acceptance rate, edit rate, deletion rate, regeneration rate, unsupported citations, and time to confirm.
- During the parallel Google Form pilot, compare omissions and factual corrections—not just prose similarity.
- Track results by prompt version and provider/model.
- Establish launch thresholds, for example zero unsupported claims in the evaluation set and a target median confirmation time.

### Report governance

- Add a correction/retraction workflow after delivery.
- Define access controls and retention for employee activity, comments, and generated reports.
- Record who confirmed, edited, regenerated, synthesized, and delivered each artifact.
- Preserve a human-readable audit view linking each report clause to confirmed ledger claims.

## Skill execution and model-provider strategy

### Recommendation

Use a provider-neutral invocation layer inside the OpenShift application, with **OpenAI Responses API as the initial production provider** and an **OpenAI-compatible in-cluster inference endpoint as the strategic fallback**. Keep Anthropic as a quality benchmark during the pilot. Do not make the Gemini hosted-agent path a production dependency yet.

For these two skills, first test whether a normal Responses API call with the versioned `SKILL.md` instructions and strict Structured Outputs is sufficient. The skills currently contain instructions only; they do not need scripts, assets, shell execution, or a hosted filesystem. If quality is equivalent, direct structured inference is simpler and cheaper than mounting a hosted skill container. Retain OpenAI hosted Skills as an optional execution mode for future skill packages that genuinely need scripts or supporting files.

### Why OpenAI is the best initial fit

- OpenAI officially supports uploaded, versioned Agent Skill bundles and hosted container execution through the Responses API.
- It also supports direct structured model responses, which better matches the application's Pydantic JSON contracts.
- The cluster only needs controlled outbound HTTPS and a project credential; no public inbound route is required.
- OpenAI's Batch API offers a 50% discount for asynchronous workloads, but its completion window is up to 24 hours. It is suitable for drafting well ahead of delivery, not for interactive Slack regeneration or a time-sensitive Monday report.
- Provider-hosted execution avoids operating GPU capacity, model serving, autoscaling, and model upgrades inside the application cluster.

### Important OpenAI caveats

- OpenAI still requires API authentication. “Hosted OpenAI skills” do not eliminate the credential requirement; use a project service account or workload identity federation when available rather than a personal API key.
- Hosted skills are mounted through a hosted shell/container. Container charges are separate from model tokens, so hosted Skills are not automatically cheaper than direct structured inference.
- Pin explicit skill versions and model snapshots. Do not use `latest` in production report runs.
- Set `store=false` where supported and complete a data-retention/security review because Jira comments and employee activity are sent to the provider. OpenAI documents no training on API data, default abuse-monitoring retention, and additional controls for approved customers.
- Confirm that the chosen lower-cost model supports the exact Skills/shell feature before rollout. The current experimental branch defaults to `gpt-4.1`, while the current official Skills examples use newer Responses API models; this compatibility must be tested rather than assumed.
- The current branch parses free-form JSON text. Prefer native Structured Outputs when compatible with the skill execution path, followed by Pydantic validation and one bounded repair attempt.

### Provider comparison

| Route | Fit | Advantages | Main disadvantages | Recommendation |
|---|---|---|---|---|
| OpenAI direct Responses + Structured Outputs | High | Simple, strong JSON contract, no hosted shell required, low operational load | API credential, egress, provider data review | Preferred default for current instruction-only skills |
| OpenAI hosted Skills | High when files/scripts are needed | Official versioned bundles, standard `SKILL.md`, hosted execution | API credential plus model/container cost; more moving parts than needed today | Keep as optional mode; benchmark against direct Responses |
| Anthropic hosted Skills | Medium-high | Existing implementation and useful quality baseline | Separate vendor/key, cost and portability concerns | Retain for pilot A/B and emergency fallback |
| Gemini Skill Registry / managed agent | Medium/experimental | Google IAM and governed registry; reusable hosted agents | Preview/Pre-GA dependencies, greater lifecycle/API complexity, GCP coupling | Research track only until stable and contractually available |
| In-cluster OpenAI-compatible endpoint | High only with existing AI platform/GPU capacity | Data remains in controlled infrastructure, no per-call external API, provider independence | GPU/platform cost, autoscaling, upgrades, observability, and potentially lower output quality | Strategic fallback; production only after quality/cost evaluation |

### Branch assessment

- `feat/openai-hosted-skills`: the strongest starting point. It adds a relatively contained provider adapter and routes both drafter and synthesizer. Before merging, rebase it onto current `upstream/main`, replace the assumed model default, use the official SDK where practical, add native Structured Outputs, and test actual hosted-Skills account/model availability.
- `feat/gemini-hosted-skills`: substantial implementation, but approximately 1,900 changed lines and multiple long-running preview APIs make it operationally expensive. The implementation also enables a wildcard network allowlist in the managed agent environment. Keep isolated until the Google services are GA/approved and narrow all network access.
- `feat/drafter-llm-backends`: demonstrates the useful OpenAI-compatible endpoint abstraction, but it diverged before current M4-M6 work and deletes CronJobs, synthesizer/reporting, migrations, and many tests relative to current main. Do not merge it. Extract only a small backend interface and endpoint client into a fresh branch based on current main.

### Target architecture

Define one interface used by both skills:

```text
invoke_structured(
  task,
  skill_version,
  instructions,
  payload,
  output_schema,
  timeout,
) -> validated result + provider metadata
```

Implement adapters for:

1. `openai_responses` — default, direct structured inference;
2. `openai_hosted_skill` — uploaded/versioned skill container;
3. `anthropic_skill` — pilot benchmark/fallback;
4. `openai_compatible` — in-cluster vLLM/KServe endpoint.

Provider selection should be configuration, not separate feature branches. Store provider, endpoint/model snapshot, skill ID/version/hash, request ID, latency, token usage, retry count, and validation outcome with every draft and report run. Do not automatically fail over between providers within one run unless the audit record clearly identifies the provider that produced the accepted output.

### OpenShift deployment pattern

- Keep CronJobs stateless; load provider configuration from ConfigMaps and credentials from Secrets or external secret management.
- Use NetworkPolicies/egress controls to permit only the chosen provider endpoint and required Jira/GitHub/Slack hosts.
- Use a dedicated provider project/service identity with spend and rate limits.
- Set active deadlines longer than the provider timeout but shorter than the gap before the next workflow stage.
- Retry only transient errors with jitter; do not retry schema/content failures indefinitely.
- Persist run state before invoking a provider so an interrupted pod can be safely resumed.
- For an in-cluster endpoint, use an authenticated KServe/vLLM OpenAI-compatible service, readiness checks, minimum warm capacity during the reporting window, and an independently deployed model-serving namespace.

### Evaluation before choosing a model

Run the same frozen evaluation set through OpenAI direct, OpenAI hosted Skills, Anthropic Skills, and the candidate in-cluster model. Compare:

- unsupported-claim rate;
- missing confirmed work and missing asks;
- evidence/link correctness;
- schema-valid response rate;
- human edit/delete/regenerate rate;
- p50/p95 latency and failure rate;
- total cost per team/week, including hosted containers or amortized GPU cost.

Make provider choice from these measurements. “Cheaper” should mean total operational cost at the required quality, not token price alone.

## Engineering backlog

### Testing and CI

- Add CI for pytest, Ruff, mypy, Alembic upgrades, container builds, secret scanning, and Kubernetes schema/policy validation.
- Add Postgres integration tests for revision history, concurrent drafts, duplicate button clicks, report persistence, and rollback behavior.
- Test Slack authorization, stale actions, repeated confirmations, modal limits, and background regeneration failure.
- Test every CronJob's schedule in its declared timezone.
- Add end-to-end fixture tests from collector payload through final Markdown.
- Add golden/evaluation tests for both skills.

### Deployment and operations

- Pin dependencies with a lockfile.
- Deploy immutable container digests.
- Add structured logs with run ID, person ID, week, stage, duration, and error class without logging sensitive content.
- Add metrics and alerts for collection failure, draft failure, pending confirmations, synthesis failure, and delivery failure.
- Write runbooks for token expiry, Jira/GitHub rate limits, Slack outages, database loss, and Monday reruns.
- Separate generated deliverables and experimental provider work from the production tree.

### Data model

- Add an orchestration run table with stage state, attempts, timestamps, and error details.
- Use constrained enums/check constraints for participation and source states.
- Add immutable entry IDs to synthesis contracts.
- Add claim-level evidence references if strict auditability is required.
- Add report category/group fields that can be human-corrected.
- Record collection coverage and truncation metadata.

## Documentation updates

- Update `docs/DESIGN.md`: Edit and Regenerate are implemented, not stubs.
- Replace “Approved for implementation” with an accurate implementation/pilot status and date.
- Update milestone/Jira status for M5 and M6.
- Document the exact production command path and report audit behavior.
- Separate local development, pilot, and production deployment instructions.
- Add a production readiness checklist and rollback procedure.
- Document all participation states and cutoff semantics.

## Suggested delivery order

1. Fix the five P0 orchestration problems.
2. Add M6 tests and CI gates so those defects cannot recur.
3. Align both skill contracts with code schemas and resolve contradictory rules.
4. Move deterministic rendering, linking, sorting, and validation into code.
5. Add immutable citations and prompt/version metadata.
6. Run a three-week controlled pilot with measurable quality criteria.
7. Harden security, monitoring, recovery, and governance before broader rollout.

## Change log

- **2026-09-22:** Initial repository and workflow audit; added detailed drafter and synthesizer skill review.
- **2026-09-23:** Added hosted OpenAI, Anthropic, Gemini, and in-cluster inference strategy and branch assessment.
- **2026-09-23:** Found a drafter gap for weeks containing GitHub PR activity but no
  collected Jira issues. The skill could emit flags while returning no entries,
  `previous_entries` could leak stale evidence into the current week, and the
  evidence filter accepted unknown HTTP URLs. Recommended and implemented a
  repository-grouped PR-only fallback, explicit current-week evidence rules,
  strict URL grounding, clearer stale-evidence warnings, and regression tests.
