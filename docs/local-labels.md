# Local labels

[Back to the README](../README.md) · [Command and configuration reference](reference.md)

## Install the optional runtime

Install the label extra before configuring a model:

```bash
uv tool install "agent-usage-tracker[label] @ git+https://github.com/rhnfzl/agent-usage-tracker"
```

This adds PostgreSQL support and, on Apple silicon, the MLX runtime and Hugging Face tools.
GGUF models also need a separate `llama-server` installation. Model weights are a separate download.
The `merge` extra supplies the one-time LoRA merge tools for `evalengine-4b`; collection never needs it.
Use `uv tool install ".[label]"` from a checkout, or `pipx install ".[label]"` if you use pipx.

## Local labels (`aut label`)

Optional. MLX models require Apple silicon; GGUF models use a separate `llama-server` runtime.
The model reads the text on this machine and only the label is sent.
`daily_note` is the exception: it sends the short note the model writes from a day's counts.
Saved calibration applies only to its exact task and prompt version. Older choices run
uncalibrated until explicitly replaced. Episode-based day tasks wait for a known rebuild.

The steps below create and evaluate a calibrated model choice. They are not prerequisites for
using an existing provisional local recipe. `aut label teach` can send sampled Claude Code text
through your signed-in `claude -p` account and spend its budget. Other agents' text is excluded
from that cloud teacher. Review the [privacy rules](reference.md#privacy-model) before running it.

1. Fetch the models you want to compare once (see [The models](#the-models)).
2. `aut label teach --task steer_intent --n 200` writes silver labels.
3. `aut label review --task steer_intent` lets you answer gold labels on a local page.
4. `aut label eval --task steer_intent` scores every model that is on disk, says which it left out
   and how to fetch them, and prints a table of quality, speed and memory, with a mark on the Pareto
   frontier and on models that fail a floor. Scores are saved, so a rerun calls a model only for new
   gold and tuning needs no model.
5. `aut label choose --task steer_intent --model kev-4b` writes your pick, with its calibration and
   thresholds, into `label.json`. Without `--model` it prints the table and a recommendation and
   writes nothing. Without a choice a task runs uncalibrated and every row is marked below
   threshold. `aut label eval --task steer_intent --final --model M` is the last check, on days
   nothing else has read.
6. Add the task to `label_tasks` and set `AUT_LABEL=1` in `scan.env` for the nightly run.

Details and the stored row: [CONTRACT.md](../CONTRACT.md#labels-activity_event-kind-label-aut-label).

### Provisional historical baseline

Use existing model labels to explore work patterns without treating them as calibrated results.
The import keeps primary Sonnet categories and direct agent-reviewed signals separate. Uncertain
cases stay unknown. Charts describe estimates, not verified accuracy.

```sh
aut label import-baseline /path/to/private/working-baseline/labels.json  # preview only
aut label import-baseline /path/to/private/working-baseline/labels.json --apply
```

`--apply` appends the entire validated bundle in one transaction through `transport.pg_dsn`.
Repeated imports are idempotent. Rows use `working.steer_intent` and `working.soft_outcome`, so
calibrated task choices, their shares, and daily notes do not consume these estimates. Source,
sampling weight, unknowns, question hashes and agent-review provenance are retained. Review
reasons and local file paths stay on the Mac. No model call or calibration runs during import.
Deploy the companion sink schema and dashboard changes before using `--apply`.
The sink's private `working_label_monthly` and `working_signal_monthly` views feed estimate panels.

Git-day scans read shared clones within configured physical roots using exact, temporary trust
exceptions. They disable all Git transports and lazy fetching. They skip generated Python caches
and vendored directories. `git_days.exclude_dirs` can name exact physical paths outside your
source scope, such as another user's inaccessible storage. Other read failures prevent a write.

### Ongoing local estimates

New work can receive provisional labels from saved local decision-model recipes. These remain
separate from the frozen Sonnet sample and from calibrated labels.

```sh
aut label run --local-estimates --dry-run
aut label run --local-estimates
```

Set `label_tasks` to the required tasks and keep a `label-local.json` beside `config.toml`.
That file records version `local-decision-v1`, each task's exact prompt version, pinned local
members, saved temperatures and combining coefficients, an evaluation hash and `minimum_score`.
Only local decision backends are accepted. Nothing is fitted or downloaded during this command.
Recipes support one model (`single`), or three or five distinct models combined by voting
(`majority`) or saved weighted scores (`stack`). Weights and temperatures come from the saved
recipe; applying it does not train the models or fit a new combination.
Members run in batches, one model at a time. Scores at or below the saved minimum stay unknown;
the minimum is at least 0.5. These scores are not validated accuracy estimates.

Rows use `working.local.<task>` and policy `local-decision-v1`, with original unit timestamps,
weight 1 and model/policy provenance. They describe eligible local inputs, not all activity.
Changed input facts create a new event under the same policy, including a new unknown result.
Repeating the same unit, facts and policy stays idempotent. Consumers select the newest
`labelled_at` per task, unit type and unit ID across policies; they must retain newer unknowns.
Local estimates and new historical replay previews can read Claude Code, Codex, Cursor CLI,
and Pi inputs using their collector event IDs. Teacher/export sources remain Claude-only.
No input text is sent to the sink. Work categories also use local window blocks.
Codex automated/subagent prompts, copied fork history, Cursor summaries/subagent chats, and
copied Pi prompts are excluded. Cursor reply text is omitted because the stores lack reliable
per-reply timestamps. Missing local text stays unclassified. Saved replay snapshots are unchanged;
adding agent support does not expand an existing job's frozen inputs.
Codex's `external_agent_session_imports.json` identifies copied external history. New
collection and local label inputs exclude records through the registered import second.
Later native work keeps its existing event IDs. The index has second precision, so work
within that final second stays unavailable. An unreadable or invalid index stops the source
instead of accepting copied history. Existing stored rows and frozen jobs are retained;
consumers need an audited availability guard for classifications made from copied inputs.
Upgrades rebuild carried import state while preserving retained native ordinals. New scans
resolve the imported prefix format separately from later native records. Older releases can
already have conflicting mixed-format ordinal histories. Keep the existing watermark when
recovering those installations; a fresh reread cannot infer their earlier collection order.
Outcome inputs stay inside their own episode's time bounds. Local estimates skip incomplete
prompts or a missing attributable final reply, reporting these as unclassified rather than
using a reply to an earlier input. Replay previews report `incomplete_outcome_context` separately.
Work-category prompts and activity also stay inside their own episode. Final replies stop at
the earlier of the episode end or local midnight, so shared sessions cannot mix separate work days.
Native Codex prompt order resolves tied timestamps within one session. Conflicting replies
from simultaneous sessions do not supply an attributable final reply.
The draft `writing_task()` and `bounded_outcome_task()` are not registered. They define explicit
version-3 private evaluations; live work/outcome tasks and recipe validation remain version 2.
Each Source snapshots canonical session paths once per agent, then reads only requested files.
Text and replies still use each request's cutoff; no persistent text cache is added. A new
Source discovers files created after the previous snapshot.
Registered Cursor automation is excluded even when callers supply steer IDs or paths directly.
New registrations also invalidate cached Cursor context. The direct reader checks origin after
its message snapshot; prompt wording and one-shot usage do not establish automation.
Mature-outcome catch-up is disabled by default. After evaluating input coverage and the saved
recipe, set `label_mature_outcomes = true` and use `aut label run --local-estimates --task soft_outcome`.
This retains recent reassessment and includes older current no-code episodes with complete
bounded context. Otherwise the nightly outcome window stays fourteen days.
The private `label/outcome-coverage.json` journal stores coverage hashes, machine identity, and
the next queue phase. It advances only after delivery, including unknown results. Each phase
gets a turn across resumptions; increasing mature idle age alone does not trigger reclassification.
New source revision, input context, task version, or policy causes new coverage work. Current
no-code eligibility and revision are rechecked before delivery. Corrupt progress is not reset.
The journal uses the per-user label data directory (`~/.local/share/aut/`, or `XDG_DATA_HOME`).
POSIX files use mode 0600. On Windows, access depends on inherited directory ACLs (access
permissions); Unix mode bits do not establish privacy. Use a directory restricted to your account.
Set `label_mature_outcomes = false` to stop mature catch-up without deleting its journal or
changing the saved model recipe. This setting applies only to local estimates; teacher sources
and frozen replay jobs keep their existing scope. A dry run scores local models but writes no
coverage progress or labels. Real model-batch throughput still depends on the configured budget.
State and heartbeats use `label_local`. The frozen baseline views do not read these tasks.
For scheduled estimates, set `AUT_LABEL=1` and `AUT_LABEL_MODE=local` in `scan.env`.

### Historical local replay

`aut label replay` prepares historical labels without resetting daily collection. First choose
one task and explicit local dates. The end date is exclusive and cannot include an unfinished day:

```bash
aut label replay --task steer_intent --since 2026-10-01 --until 2026-10-08
aut label replay --job JOB_ID
aut label replay --job JOB_ID --apply --budget-s 600
```

The preview returns a private job ID, eligible request counts, and `missing_local_text` for units
excluded because this machine lacks their text. It runs no model and sends no rows. Resume the
same job with `--apply`; progress advances only after each batch is delivered. The default budget
is 600 seconds, with a maximum of 3600. An incomplete result reports `remaining` and
`complete: false`; repeat the saved job to continue. Nightly watermarks are untouched.
The budget stops new scoring work. An active model call or sink delivery can finish after it.
`partial_local_text` counts eligible episode units with some missing steer text. Counts cover
supported local source units, not all historical work. Replies and context can also be absent.

Jobs retain redacted input facts under `~/.local/share/aut/label/replay/` (or `XDG_DATA_HOME`).
This explicit local snapshot is an exception to the normal text-free label cache. Job directories
use mode 0700 and files use mode 0600 on POSIX. Windows uses inherited directory ACLs, so keep
the data directory restricted to your account. A local ignore file excludes jobs from Git. No trace text is
sent to the sink or a cloud model. Keep private jobs, references, evaluation reports and
data-derived benchmark results outside the public source repository.

Each new preview creates an intentional classification generation. Retries of that saved job are
idempotent, including after a delivery succeeded but checkpointing failed. Each preparation reserves
a strictly newer `labelled_at` under a private lock, including when preparation times are equal.
Normal local-estimate runs share this ordering clock. Calibrated labels and notes keep their existing behavior.
This timestamp stays fixed, so a delayed older job cannot replace a newer result. Changing the current task
recipe requires a new job; changing an unrelated task does not invalidate the saved job.

Replay selects original unit timestamps within the requested range and uses current facts frozen
at preparation. It does not reconstruct past episode state. Work includes eligible window blocks.
Explicit outcome replay can reach older units beyond the nightly fourteen-day selector, but
still includes only currently eligible no-code episodes. Publishers must check current episode
eligibility and select newest labels across policies, retaining newer unknowns.


### The models

`--model` and `label_model` take these names. Any other name is read as an MLX repo through answer
letters, the way Gemma is.

| Name | Weights | Runs on | Size | s/item | Memory | Right of 20 | Licence |
|---|---|---|---|---|---|---|---|
| `elephant-1` | community: 0x36's 4-bit port of baselabs/elephant-1 | MLX | 5.1 GB | 0.29 to 0.60 | 5.5 GB | 20 | MIT |
| `evalengine-4b` | official LoRA merged on official Qwen3.5-4B, bf16 | MLX | 8.4 GB | 0.19 to 0.35 | 8.8 GB | 20 | Apache-2.0 |
| `clef-flash` | community: mlx-community's 4-bit port of Cloudflare/clef-flash | MLX | 6.2 GB | 0.36 to 0.62 | 5.9 GB | 20 | Apache-2.0 |
| `aurelis-2b` | official q8_0 GGUF | `llama-server` | 2.7 GB | 0.09 to 0.43 | 3.1 GB | 20 | Apache-2.0 |
| `pplx-decider-flash` | official bf16 | MLX | 9.1 GB | 0.13 to 0.33 | 8.6 GB | 19 | Apache-2.0 |
| `kev-4b` | community: ggml-org's q8_0 conversion of jaredpalmer/kev-4b | `llama-server` | 4.5 GB | 0.12 to 0.28 | 5.1 GB | 20 | Apache-2.0 |
| `gemma-4-31b` | community: mlx-community's 4-bit build | MLX | 18.4 GB | 2.75 | 17.7 GB | 20 | Apache-2.0, per its card |

Measured on one Apple silicon Mac, on 20 easy synthetic cases, one model at a time. The low s/item
is aut's own backend after a warm-up; the high one is an earlier run on a busier machine. These
numbers are not a ranking: which model is best on your work is what `aut label eval` is for. Memory
of the two `llama-server` models is the server's RSS, which can miss Metal buffers.
`gemma-4-31b` scored 7 until aut turned thinking off: its chat template opens a thinking channel by
default, and the answer letters were then read at a position where the model expects `<|channel>`.

Fetch once, one at a time. A nightly run never downloads:

```bash
hf download mlx-community/gemma-4-31b-it-4bit
hf download 0x36/elephant-1-9B-MLX-4bit --revision 16de0e500eea4e44fc7db79928c03c53e324bcb0
hf download mlx-community/clef-flash-4bit --revision b5adbe2c3a95200d4bf04a0727c8ec457157d5e1
hf download perplexity-ai/pplx-decider-flash-v1.2 --revision 9ff7cf9838e59df461c276b7699d56b244d2af58
hf download Leptos-ai/Aurelis-2B aurelis-2b-q8_0.gguf --revision 1b56513618a9de9378440d3bca5e4e163f142279
hf download ggml-org/Kev-4B-GGUF Kev-4B-Q8_0.gguf --revision f4624b6bb7072f1bab726a622c1cad3f96030b9e
```

`evalengine-4b` is a LoRA, so it is merged once onto its base and saved under
`~/.local/share/aut/label/models/`. This loads the 9 GB base on the CPU and needs torch for this
command only. After it, the model needs only `mlx-lm`, and you can delete the base from the cache:

```bash
hf download evalengine/decision-4b --revision aadc55517ae8bfb1264fd5932ec452b939361e47
hf download Qwen/Qwen3.5-4B --revision 851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a
# from a checkout:
uv run --isolated --no-project --with ".[merge]" python -m agent_usage_tracker.label.merge evalengine-4b
```

`aurelis-2b` and `kev-4b` need `llama-server` from llama.cpp, build 11669 or newer
(`brew install llama.cpp`; `llama-server --version` shows the build). The official GGUF of
`pplx-decider-flash` does not run on it (HTTP 501), so that model runs on MLX from the official
weights. The `clef-flash` GGUF does run, but its probabilities differ from the MLX build's by up to
0.27 on the same top answers, so aut uses the MLX build.
