# Investigate coding-agent sessions with AUT

Search across agent sessions, cite captured events, and compare a failed attempt with a clean one.
Use the CLI from a shell or give an agent the same operations through MCP. Both read a selected
audit or a portable evidence bundle.

## Capture a project

```sh
aut evidence capture --project /path/to/repo --days 7 --limit 20
aut evidence --audit AUDIT_ID describe
aut evidence --audit AUDIT_ID search --status error
aut evidence --audit AUDIT_ID search --query "Operation not permitted"
aut evidence --audit AUDIT_ID search --mode ranked --query "permissions cache"
aut evidence --audit AUDIT_ID session SESSION_ID
aut evidence --audit AUDIT_ID event SESSION_ID EVENT_ID
aut evidence --audit AUDIT_ID commits COMMIT_SHA
aut evidence --audit AUDIT_ID compare FAILED_SESSION_ID CLEAN_SESSION_ID
```

Exact mode is the default. It matches literal substrings. Ranked mode uses SQLite FTS5 to match words
in any order and puts exact phrases first. A running MCP server reuses an in-memory index for
queries and pages that use the same filters. It rebuilds the index when the filters or source
files change. Each CLI invocation builds a fresh index. Narrow the filters if the index would
exceed 50,000 events or 64 MiB. Metadata-only scope supports filters without text search.

Copy the full audit, session, and event IDs from the JSON results. Follow search's `next_cursor`
with the same filters. An empty search page can still have a cursor when the scan budget runs out.
Session results list event references. Supported native Codex turns link the completed turn to its
recorded model and workspace. Metadata-only clients can see the status and source location.
Read a referenced event and follow `next_offset` to read its
remaining text. Commit lookup cites a matched Git call and result. A short recorded prefix can
match more than one commit. AUT reports the prefix as captured. Redacted and metadata scopes omit
command-derived IDs that match a redaction rule. Comparison shows event counts and the first difference in the observed tool
sequence. A missing result stays unknown. The comparison does not infer why two runs differed.

For a completed T3 run with a ready workspace checkpoint, AUT resolves its local Git ref
when it captures the audit. Open the boundary event to see the checkpoint time and, with
full or redacted access, the ref and commit. This is the workspace state saved by T3 after a run.
Ordinary-work studies still verify turn-start setup from the evidence recorded for that turn.
If you already have a checkpoint SHA, use
`aut evidence --audit AUDIT_ID commits CHECKPOINT_SHA --source checkpoint` to find its run.
MCP accepts the same `source` option.

The project filter includes its Git worktrees and excludes sessions with recorded directories in
other repositories. T3 applies the thread limit within that project.
The filter cannot infer where unrecorded tool activity took place. Select explicit
sessions before sharing externally. Use `--workspace` or `--tool` to filter on recorded labels. Capture uses AUT's existing Claude Code, Codex, Pi,
Cursor CLI, and T3 Code sources and preserves their coverage limits. It skips recently active
sources by default. Session and event IDs refer to frozen evidence. A fresh capture can include
new events and produce a new ID.

If a session reports a byte cap, capture the project again with a larger per-source budget.
For example, use `aut evidence --content redacted capture --project /path/to/repo --max-mib 64`.
The default is 16 MiB. The audit records the budget and reports any remaining capture gaps.

## Choose what the client can read

Put `--content` before the operation, for example
`aut evidence --audit AUDIT_ID --content redacted event SESSION_ID EVENT_ID`.

| Mode | Result |
| --- | --- |
| `full` (default) | Unredacted text available from the selected capture. |
| `redacted` | Text with recognized secrets and identifiers masked. Review it before sharing. |
| `metadata` | Event references, kinds, counts, and coverage. Text search is unavailable. |

Running `aut evidence capture` authorizes a new private copy of available text in the AUT improvement
store. It retains normalized text, subject to each source's capture budget and exclusions.
Existing audits may contain only clipped, redacted text. Such events report `snapshot_redacted`
when the client requests their text. This includes a restored snapshot without its full-text record.
Capture the source again if it is still available.

Full text lives in separate private records. Existing judges, drafts, and the usage sink still
receive their existing inputs. Default restic backups exclude these records in the standard store
and the store named by `AUT_IMPROVE_ROOT`. Add the full-content directory to `backup_excludes`
when a different `--root` sits inside a backed-up path. Use `aut evidence --content redacted capture` to save only the
standard redacted snapshots. Bundles and MCP clients can narrow their content mode; they cannot
recover text omitted at capture or export. A local process can still pass a response to a hosted
model through its client. Choose the content mode and client together.

## Connect a local MCP client

Install the optional MCP extra in the environment that supplies `aut`.

```sh
pip install 'agent-usage-tracker[mcp]'
```

Set the client's MCP command to the absolute path to `aut`. Pass these arguments:

```json
["evidence", "--audit", "AUDIT_ID", "mcp"]
```

Use the same `--root` as capture if you use a custom store. Add `--content redacted` before
`mcp` to mask text. The server exposes `describe`, `search`, `session`, `event`, `compare`,
`commits`, `case`, and `ordinary_work` with the same scope and results as the CLI.
It provides read operations only.

An example prompt for the connected agent:

> Find recent setup failures and a comparable successful attempt. Cite the failed call and its
> result, list the differences you can observe, and propose one change we can test. State any
> missing context that affects the diagnosis.

## Read the evidence from another machine

Export the exact sessions you want to share:

```sh
aut evidence --audit AUDIT_ID --content redacted export investigation.json SESSION_ID OTHER_SESSION_ID
aut evidence --bundle investigation.json search --status error
```

Transfer the file through a channel you choose. A bundle is private plain JSON with an integrity
checksum. It can be opened without the original logs or store. AUT refuses to overwrite an existing
bundle. Treat a bundle as saved data. Revoke client access by stopping the server or replacing its
token. Copies recipients already have remain readable.

On a private host, load a bundle or use an audit already on that host. Supply a unique random
URL-safe token of 32 to 128 characters through `AUT_EVIDENCE_TOKEN`. For example, generate it with
`python -c 'import secrets; print(secrets.token_urlsafe(32))'` and store it with the service's secrets.

```sh
aut evidence --bundle investigation.json mcp --transport http --port 8765
ssh -N -L 8765:127.0.0.1:8765 user@host
```

Run the first command on the evidence host and the second on the client machine. Configure a client
that supports custom bearer headers to use `http://127.0.0.1:8765/mcp` with that token. The service
binds to loopback and scopes one token to the selected evidence. It supports explicit bearer
credentials over the tunnel. It does not run an OAuth authorization server or a multi-tenant
service. The host's normal AUT usage sink does not provide transcript access.

## Turn an observation into a repair

Freeze selected attempts in the private store. This creates the same type of case AUT uses for
proposals and studies. It does not run a judge.

```sh
aut evidence --audit AUDIT_ID freeze-case FAILED_SESSION_ID FAILED_EVENT_ID
aut evidence --audit AUDIT_ID freeze-case CLEAN_SESSION_ID CLEAN_EVENT_ID
aut evidence --audit AUDIT_ID compare FAILED_CASE_ID CLEAN_CASE_ID --cases
```

A case from a successful run can preserve a workflow worth reusing. Case results list commit IDs
observed in matched Git tool results when the capture contains them. Use the failing case when
drafting a repair. Set an explicit scope and target for the proposed change.

```sh
aut improve propose --case FAILED_CASE_ID --scope /path/to/repo \
  --target /path/to/repo/AGENTS.md --before-file before.txt --after-file after.txt
```

Use the same improvement root for both commands. The
[study workflow](improvement-studies.md) links the candidate to the original evidence. Test an
isolated baseline and candidate with the same acceptance check. Record the result and compare
fresh eligible work before claiming a lasting benefit. The source configuration changes only
through the existing review and adoption process.

## Propose a project skill from successful work

Open the successful attempts and freeze their case IDs. Ask the reviewing agent to write a draft
`SKILL.md` with a matching `name`, a `description`, and the instructions. Cite at least two independent
sessions from the same project. Each citation identifies a successful result matched to a captured
call. AUT records the supporting cases and any observed commit IDs.

```sh
aut evidence --audit AUDIT_ID propose-skill --scope /path/to/repo \
  --target .agents/skills/check-setup/SKILL.md --draft-file draft.md \
  --proof CASE_ID:EVENT_ID --proof OTHER_CASE_ID:OTHER_EVENT_ID \
  --candidate /path/outside/the/repo
```

Review the proposal with `aut improve inbox list` and the existing review workflow. AUT can prepare
an addition or update, and stage it in the independent candidate. A new file has a null baseline
hash. To trial the addition, explicitly list the file with a null value in `files.baseline`. List its
SHA-256 hash in `files.candidate`. This binds the trial to the absence of the skill before the change.
A successful tool result is evidence for the cited step. Review whether the proposed instructions
apply to the whole workflow before you adopt the skill.
