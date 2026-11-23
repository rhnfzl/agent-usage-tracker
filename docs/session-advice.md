# Get a next step when pytest fails to collect tests

AUT can suggest checking the project’s CI test command when pytest reports an import error
during collection. The suggestion points to the committed workflow. Use it to check for a
difference in the test invocation before editing application code.

## Replay a captured result

Create or select a private audit and identify the failed result and its session.
Use the full commit ID that applied at the time of the failure.

```sh
aut evidence --content redacted capture --project /absolute/path/to/repo --no-observe
aut evidence --audit AUDIT --content redacted search --status error
aut advise replay --audit AUDIT --session SESSION --event RESULT \
  --project /absolute/path/to/repo --revision FULL_COMMIT_ID
```

Replay uses the selected result and its earlier tool call. It returns a suggestion with
evidence references or a quiet status. It does not write a receipt or run any test command.
The supplied commit pins the CI input. AUT does not infer which commit was checked out.

## Connect your coding agents

Install AUT once, then preview the user-level hook changes on macOS or Linux. The preview
lists detected clients. Repeat `--client` to select specific clients.

```sh
uv tool install agent-usage-tracker
aut advise setup
aut advise setup --apply
```

All clients call the same installed `aut` executable. Setup keeps other hooks and backs up
existing JSON settings before editing. Start or reload the client from a Git project root.

- [Claude Code](https://code.claude.com/docs/en/hooks#posttoolbatch) uses a user-level PostToolBatch hook.
- [Codex](https://learn.chatgpt.com/docs/hooks) uses a user-level PostToolUse hook. Open `/hooks`
  and review and trust the AUT entry. Codex asks for review again when the hook definition changes.
- [Cursor](https://cursor.com/docs/hooks) uses its Shell result hooks. A hook needs complete terminal
  output to detect the failure. Cursor Tab uses a different interface.
- [Pi](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
  loads a small user extension. Restart Pi to load the extension.

The default mode records observations. Run `aut advise setup --apply --mode show` to offer one
suggestion per client, project and session. The hook leaves the tool result in place and never
executes the suggestion. Local configuration applies to sessions that run on this machine.

The pilot reads foreground shell calls that invoke pytest directly or through `python -m pytest`.
It looks for one supported inline console pytest command in the committed
`.github/workflows/ci.yml` under `jobs.test.steps`. It skips shell pipelines, custom working
directories, ambiguous CI commands, and unsupported result formats. Live advice uses `HEAD`.
If the CI command already ran in the same batch, the hook stays quiet.

To disconnect, remove the AUT entries from the client paths shown by setup. For Pi, remove
the generated `aut-session-advice.ts` extension. Existing receipts stay available. For a manually
configured project hook, pass `--project /absolute/path/to/repo` to `aut advise hook`.

## Review the outcome

Open `aut overview` and find **Session advice**. The table shows the last 30 local observations.
It includes the next recognized pytest result and the first observed run of the suggested command.
Multiple pytest calls in the next batch leave their order unknown. A passed summary is a report
from pytest. Use your task’s acceptance criteria to decide if the work is complete.

After that independent check, record its result with the observation ID shown in the overview.

```sh
aut advise acceptance OBSERVATION --result accepted
```

Use `--result rejected` when the work fails the acceptance check. The overview labels the result
as operator review. Until then, acceptance stays unknown. A retry that passes does not establish
that the suggestion made the work faster or changed the outcome.

Receipt files live under `$XDG_DATA_HOME/aut/session-advice`, or
`~/.local/share/aut/session-advice` when `XDG_DATA_HOME` is unset. Set `AUT_ADVICE_ROOT` for
both the hook and overview to use a different private directory. The hook stores the verified CI
command, mode, timestamps, outcome categories, and hashed references. It does not copy transcript
text, tool output, or the original command into the receipt. It runs locally without a model.
