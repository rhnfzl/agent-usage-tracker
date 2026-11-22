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
`commits`, `case`, `ordinary_work`, and `retries` with the same scope and results as the CLI.
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

## Follow linked tool results

Codex native logs can contain web-search results that the T3 projection omits.
AUT uses a native result only when its recorded item ID matches one result in the corroborated
native turn. The result has a `source_result` reference to its native event. Select both
snapshots when exporting a bundle that needs the reference. The T3 case keeps the redacted text.
Read the native event with full content access if you need the captured original.

Claude Code web-search result blocks are captured directly when present. Pi and Cursor records
with a missing result payload leave a tool gap. An unlinked or ambiguous search result leaves a gap. A clipped result keeps its reference
and still leaves the T3 run incomplete. Other gaps still apply when a result is recovered.

## Find retries across sessions

```sh
aut evidence --audit AUDIT_ID retries
aut evidence --audit AUDIT_ID retries --tool Edit --minimum-sessions 1
```

The query groups retries by agent and tool. It counts occurrences and distinct source sessions.
The default requires two distinct source sessions. Duplicate incidents in snapshots count once. An observed result takes precedence over a missing
one. If snapshots show conflicting observed outcomes, the query excludes that incident and
increments `conflicting_incidents`.
Divergent snapshots and linked supplemental sources do not contribute to the counts.
The result shows identical and changed-argument match counts and the observed result statuses.
A group can contain several causes. Use its example event IDs to inspect the original failure
and intervening actions before choosing a case.

The query returns up to ten examples per group from distinct sources. Capture gaps can hide
other retries. Metadata mode omits tool names. This query requires a local audit because evidence
bundles omit the private request fingerprints.

Freeze a discovery case with the example's `session_id` and `error_result`.
Find a comparable successful attempt and freeze its result as a control.
Use one acceptance criterion across both in a [private study](improvement-studies.md).

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

## Run a programmable investigation

A recipe can use evidence from one session to decide what to search for in another.
Give it explicit audits or bundles, then return citations, a timeline, a hypothesis,
or a statistic with its counting rule.

```sh
pip install 'agent-usage-tracker[code]'
aut evidence code-catalog
aut evidence --audit FIRST --content redacted run-code follow.py --include-audit SECOND --save
```

For example, put this program in `follow.py`. It takes a lead from a match in the
first audit and searches the second. The last expression becomes the JSON result.

```python
seed = read("search", query="setup failed", limit=1)
found = []
if seed["data"]["items"]:
    query = seed["data"]["items"][0]["preview"].split()[-1]
    cursor = ""
    while cursor is not None:
        page = read("search", scope="2", lead=seed["receipt"], query=query, cursor=cursor)
        for hit in page["data"]["items"]:
            found.append({"session_id": hit["session_id"], "event_id": hit["event_id"]})
        cursor = page["data"]["next_cursor"]
found
```

Use `-` in place of the path to read source from stdin. Pass a JSON object with
`--params` to populate the program's `params` variable. The `scopes` variable lists
the actual coverage and content grant per source. Scope `1` is the main audit or
bundle. Additional audits come next, followed by additional bundles. Each type
retains command-line order. Bundles can narrow the content grant. Code mode
defaults to redacted access. Existing direct commands keep their own defaults.

The catalog lists the read operations, their parameters, and defaults. Set `lead`
to an earlier read's receipt to record which observation motivated a later read.
Each successful read records available event references, a result digest, and
whether a further page was offered. Continue an empty page if it has a cursor.
Check scope and session coverage before drawing conclusions. A returned citation
identifies an observed record. A person or a reviewed rule must check what it
supports. Snapshot IDs may repeat across audits or change after a recapture.
When comparing distinct source sessions, use an available source identity and
report cases where it is unknown. The readers expose recorded Git outcomes and
study evidence. They require independently verified links for joins with usage
or records outside the selected evidence.

Recipes run in fresh local Monty sessions. The runner limits source size, host
calls, evidence bytes, output, and wall time. Failed execution reports an error.
Recipes use the Python subset supported by Monty. They get no general host
filesystem, network, or shell operations. Plan a stopping rule for recursive
searches, and report which leads you left open if you stop early.

Save a run with `--save` to keep its program, parameters, scopes, and read receipts
in the private AUT store. Saving can retain sensitive text in a program or its
parameters. The default AUT backup exclusions cover this directory under the
standard store and `AUT_IMPROVE_ROOT`. Add a backup exclusion when using a different
`--root` inside a backed-up directory.

```sh
aut evidence --audit FIRST replay-code RUN_ID --include-audit SECOND
aut evidence --audit NEW_FIRST run-code --recipe RUN_ID --include-audit NEW_SECOND
```

Replay checks the original scope and compares the result and read record with the
saved digest. Deleted inputs can prevent replay. Changes in AUT or Monty versions
can change the digest. Use `--recipe` to run the same program on new inputs and
optionally pass new parameters.

Install `agent-usage-tracker[mcp,code]` to use a recipe through MCP. If you use
the pinned-wheel runtime installer, pass `--code` to include both optional dependencies. Add `--code`
to the server arguments after `mcp` and include any additional scopes there.
The server adds `code_catalog` and `run_code(source, params)`. The client receives
the result under the connection's grant. Restart the MCP server to select a
changed bundle. A client connected to a hosted model can
forward that result. Session observability settings govern a separate delivery
path.

## Send session evidence to a configured receiver

Set `AUT_SESSION_OBSERVABILITY_URL` to an HTTPS endpoint that accepts AUT evidence
bundle JSON by POST. Loopback HTTP is available for local development. Set
`AUT_SESSION_OBSERVABILITY_TOKEN` if your receiver requires bearer authentication.
AUT uses a dedicated endpoint for session evidence. The existing usage sink
continues to receive usage data.

With a destination configured, `aut evidence capture` sends the new audit with
redacted content by default. Set `AUT_SESSION_OBSERVABILITY=metadata` for event
references and coverage, `full` for available raw session text, or `off` to stop
delivery. Use `capture --no-observe` to keep a single capture local. A capture or
bundle can further restrict what is sent. Redaction masks recognized patterns.
Review the content policy and the receiver's retention before sharing private work.

A delivery failure appears in the capture's JSON as `observability.status=failed`.
The private audit remains available. Retry with
`aut evidence --audit AUDIT_ID observe`, or select a bundle to deliver a smaller
set of sessions. The request carries an `Idempotency-Key` that matches the bundle
digest. A receiver can use it to deduplicate identical deliveries.
