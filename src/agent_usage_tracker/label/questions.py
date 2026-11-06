"""Task version 3: one yes/no question per label, in the shape of a structured `noul` request
(task, context, evaluation rules; a true definition with qualifying evidence; a false definition
with a do-not-count list that names the neighbouring labels).

Version 2 (one multi-class question per task, a short phrase per label) stays in tasks.TASKS and
is what the nightly run still asks. These questions are for the per-label binary mode and for
re-labelling the gold with sharper definitions. The do-not-count lists settle the cases where two
frontier labellers disagreed under version 2. No unit text, project, repo or person name belongs
here: this file is public.

`other` (and the outcome group `closed`) has no question of its own: it is what is left when no
question fires. Its definition is still here, because a multi-class labeller needs it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VER = 3


@dataclass(frozen=True, slots=True)
class Question:
    label: str                       # a leaf label id of the task, or a public group id
    task: str                        # the yes/no question, phrased so yes means the label applies
    definition: str                  # criteria.true
    evidence: tuple[str, ...]        # criteria.true.examples_of_qualifying_evidence
    not_definition: str              # criteria.false
    do_not_count: tuple[str, ...]    # criteria.false.do_not_count: each names the label it is


STEER_CONTEXT = (
    "One message a developer sent to their own coding agent. `text` is the message (its first "
    "1,500 characters). `agent_reply_before` is the end of the agent's last reply before it, "
    "`previous_text` the developer's previous message in the session, `index_in_session` its "
    "position (0 is the first), `seconds_since_previous` the gap, `via` how it was sent (typed, "
    "slash_command, suggestion_accepted, queued, skill, bash_input), and `after_interrupt` "
    "whether the developer stopped the agent just before.")
STEER_RULES = (
    "Judge the developer's own words in `text`. Text the developer quotes or pastes (inside "
    "backticks, a pasted block, an image) is context; what the developer says about it decides.",
    "Read `agent_reply_before` to see what the message reacts to: a question the agent asked, a "
    "proposal waiting for a go-ahead, a result it reported.",
    "Ask what the agent is expected to do after this message: start new work, take the next "
    "step, explain, fix a mistake, change course, check something, nothing more, or stop.",
    "A slash command is judged by what it asks the agent to do. A command that only manages the "
    "session (compact, login, clear, a model or plugin switch) asks nothing about the work.",
    "Base the judgment only on visible text. Do not assume the agent erred unless the message "
    "says so or clearly implies it.",
)

WORK_CONTEXT = (
    "One unit of a developer's work. An `episode_day` is one day of one piece of agent work: "
    "`first_steer` and `last_steers` are the developer's messages that day, `last_agent_reply` "
    "the end of the agent's reply after the last one, and the counts are `steers`, `sessions`, "
    "`tool_calls_by_tool`, `tool_errors`, `interrupts`, `rejections` and `commits`. An "
    "`aw_block` is a stretch of desktop window time: `category`, `apps`, `minutes`, `titles`, "
    "`hour`.")
WORK_RULES = (
    "Judge what most of the time produced or decided, not the first message alone. Messages like "
    "'continue', 'push' or a login say nothing about the kind of work.",
    "Use the counts as evidence: many edits and commits point at producing or fixing code; reads "
    "and searches with no edits point at finding out or judging; chat and message tools point at "
    "communicating.",
    "Shipping steps (push, merge, deploy, watching CI) do not decide the label on their own: label "
    "the work being shipped. A day of shipping steps only, with nothing else visible, is building.",
    "Coordinating other agent sessions is not communicating with people: label the work those "
    "sessions do.",
    "Base the judgment only on visible facts.",
)

OUTCOME_CONTEXT = (
    "One piece of a developer's agent work that made no commit, as it stands now. `last_steers` "
    "are the developer's last 5 messages, `last_agent_reply` the end of the agent's last reply, "
    "`days_idle` the days since its last activity, `sessions` and `steers` the counts.")
OUTCOME_RULES = (
    "Judge where the work stands at its last visible exchange, mostly from `last_agent_reply` and "
    "the last message before it.",
    "An optional extra the agent offers ('want me to also ...?') does not keep the work open. A "
    "step the work itself still needs does: an unanswered decision the agent needs, a request "
    "of the developer's that was not done, a listed next step.",
    "`days_idle` alone does not decide: old work with a stated next step is still open, and "
    "fresh work that was answered is resolved.",
    "Work with no visible request (only a login or a session command, no reply) has nothing "
    "left open: it is resolved.",
    "Base the judgment only on visible text.",
)

STEER: tuple[Question, ...] = (
    Question(
        "framing", "Does the message start a new piece of work: a new goal or topic, with the "
        "background the agent needs for it?",
        "The message opens work the agent was not already doing, or a new topic in the session, "
        "and sets out what it is about.",
        ("The first message of a session or of a new topic, stating a goal or problem.",
         "Pastes an email, ticket, note, log or screenshot from outside and asks for help with "
         "it, when the session was not already on it.",
         "Asks an open question that starts a new line of thought ('how could we have done X?', "
         "'why should I not sign this?')."),
        "The message continues work already under way, or asks nothing new.",
        ("The next step of the work already under way, a go-ahead or a choice between the "
         "agent's options (that is instruction).",
         "Asking the agent to explain its own output or report status (clarification).",
         "Changing the approach or goal of the current work (redirection).",
         "Asking the agent to check or judge something it or someone made (review).",
         "Session housekeeping, a greeting or a test message (other)."),
    ),
    Question(
        "instruction", "Does the message tell the agent to take a next action inside the work "
        "already under way?",
        "The developer asks for an action (do, write, run, fix a small thing, commit, push, "
        "continue) that keeps the current goal and plan.",
        ("A go-ahead for an action the agent proposed: 'go', 'yes, do it', 'yes, push'.",
         "Choosing one of the options the agent offered, or its recommendation, so it can go on.",
         "A new concrete step or detail for the same goal ('also add the endpoint urls', 'file "
         "a ticket for it', 'package it so I can share it').",
         "'continue', or a command that runs a workflow on the current work."),
        "The message asks for no new action in the current plan, or it changes the plan.",
        ("Opening a new goal or topic with its background (framing).",
         "Asking what something means, why, how, or what the status is, with no action asked "
         "(clarification).",
         "Saying the agent's last output or action is wrong (correction).",
         "Replacing the current approach, plan or goal (redirection).",
         "Asking the agent to check, verify or judge work (review).",
         "Signing off on finished work, or reporting it done, with no further action (acceptance).",
         "Telling the agent to halt with no new direction (stop)."),
    ),
    Question(
        "clarification", "Does the message ask for or give information about the current work, "
        "without asking for an action and without claiming a mistake?",
        "An exchange of facts about the work under way: the developer asks the agent to explain, "
        "or answers a question of fact the agent asked.",
        ("Asks what the agent's words mean, or to say it more simply ('what does this mean?', 'I "
         "still don't understand this').",
         "Asks for status, progress, cost or time left ('what is the status?', 'how much has "
         "been spent?', 'how many PRs are left?').",
         "Asks whether the developer must do something ('do I need to run the dry run?').",
         "Answers a question of fact the agent asked, or adds a missing fact, with no action "
         "asked."),
        "The message asks for an action, claims a mistake, or shares no information.",
        ("A go-ahead, a choice between the agent's options, or a next step (instruction).",
         "A question that opens a new topic (framing).",
         "Asking the agent to verify or judge whether work is right or good enough (review).",
         "A question that complains the agent is doing it wrong ('why are you not converging?') "
         "(correction).",
         "Approval of finished work with nothing asked (acceptance)."),
    ),
    Question(
        "correction", "Does the message say that something the agent just did, said or "
        "produced is wrong, and want it fixed, keeping the same goal?",
        "The message points at a specific error in the agent's last output, action or claim, or "
        "objects to how the agent went about the work.",
        ("States that a result, claim, file or command from the agent is wrong ('no, the path is "
         "src/', 'this is a feature flag, not every company has it').",
         "Reports that the agent's change fails: a failing build, test or run, often with the "
         "log pasted.",
         "Rejects part of a draft and says what it should say instead, for the same task.",
         "Points out a broken constraint, or complains about how the agent works ('you ran "
         "dozens of agents for this', 'why is this taking so long?')."),
        "The message does not claim the agent's last output or conduct is wrong.",
        ("Changing the goal or approach when the old one was not wrong (redirection).",
         "Telling the agent to stop with no fix asked (stop).",
         "A next step after work that was fine (instruction).",
         "A neutral question about the agent's output, with no claim it is wrong (clarification).",
         "Approval, or approval with a small addition (acceptance)."),
    ),
    Question(
        "redirection", "Does the message replace the current approach, plan or goal with a "
        "different one, without saying the old one was a mistake?",
        "The developer drops or changes what the agent is doing or about to do, and says what "
        "to do instead.",
        ("'Instead, do X', 'let's not do that, try Y'.",
         "Removes a part of the plan or narrows it ('keep embeddings only, no keyword search').",
         "Asks for an approach the agent did not offer, or changes the order of the plan ('do B "
         "before A')."),
        "The message keeps the current approach and goal.",
        ("Saying the agent's output is wrong and must be fixed for the same plan (correction).",
         "Halting with no new direction (stop).",
         "A go-ahead or a choice between the agent's options (instruction).",
         "Starting unrelated new work after the current work ended (framing)."),
    ),
    Question(
        "review", "Does the message ask the agent to check, verify or judge work that already "
        "exists?",
        "The developer wants a judgment of an artifact's quality or correctness: code, a PR, a "
        "document, results, a claim, a plan.",
        ("'Review this PR', 'check my comments in this file', 'have you checked the PR?'.",
         "Asks whether something holds up ('is this hint general or overfitted?', 'does this "
         "ticket still hold?').",
         "Passes on another reviewer's or agent's finding and asks whether it is real.",
         "Asks to verify that a fact or list is complete or right ('so these items are all in "
         "the list, right?')."),
        "The message asks for no check or judgment of existing work.",
        ("Asking what something means or what the status is (clarification).",
         "Asking the agent to fix review findings it already has (instruction).",
         "Saying the work is wrong (correction).",
         "Approving work as fine (acceptance)."),
    ),
    Question(
        "acceptance", "Does the message sign off on finished work, or report it done, with no "
        "further action asked of the agent?",
        "The developer approves or confirms work already done or a result already reported, or "
        "closes the topic, and asks the agent for nothing more.",
        ("'Thanks', 'looks good', 'perfect', 'that makes sense'.",
         "Reports that the developer did the last step outside the agent ('the PR is merged', "
         "'done', 'I merged both').",
         "Confirms the agent's conclusion with no new request."),
        "The message asks the agent to do, explain or check something, or does not approve.",
        ("A go-ahead for a proposed action, or a choice between options, so the agent goes on "
         "(instruction).",
         "Approval followed by a new step ('looks good, now push') (instruction).",
         "Answering the agent's question of fact (clarification).",
         "Asking the agent to check the work (review).",
         "Halting work (stop)."),
    ),
    Question(
        "stop", "Does the message tell the agent to halt, pause or abandon the work, with no new "
        "direction?",
        "The developer stops the agent and gives no replacement task.",
        ("'Stop', 'wait', 'hold on', 'never mind', 'don't run the workflows'.",
         "Asks the agent to kill its processes or end cleanly.",
         "Sent right after an interrupt with no new request."),
        "The message does not halt the work, or it halts it and gives a new direction.",
        ("Stopping one approach and naming another (redirection).",
         "Stopping to fix a mistake (correction).",
         "Closing finished work happily (acceptance).",
         "Pausing for a question to be answered first (clarification)."),
    ),
    Question(
        "other", "Does the message fit none of the other labels?",
        "The message does nothing to the agent's work.",
        ("Session housekeeping commands (compact, login, clear, a model or plugin switch).",
         "A greeting, a test message, or pasted content with no request at all.",
         "A message about something unrelated to the agent's work."),
        "The message fits one of the other labels.",
        ("A short go-ahead such as 'go' or 'yes' (instruction).",
         "A short question about the work (clarification).",
         "'merged' or 'done' after the agent's work (acceptance).",
         "A slash command that runs a task on the work (instruction or framing)."),
    ),
)

WORK: tuple[Question, ...] = (
    Question(
        "planning", "Was this time mostly spent deciding what to do?",
        "The main output is a decision about the work ahead: a plan, a spec, priorities, a "
        "choice between approaches, what to say or do in a coming meeting.",
        ("Writes or revises a plan, spec, design or handoff of next steps.",
         "Weighs options and picks one, or orders what comes next.",
         "Asks what to do next, or how to approach a meeting or decision."),
        "The main output is not a decision about the work ahead.",
        ("Finding out facts about an outside system to make a decision (research).",
         "Producing the code or content the plan calls for (building).",
         "Writing the message, ticket or reply to people (collaboration).",
         "Judging work already made (review)."),
    ),
    Question(
        "research", "Was this time mostly spent finding out facts about an outside tool, "
        "system or question, to decide something?",
        "The main output is knowledge gathered to inform a choice: comparing options, reading "
        "docs or papers, measuring how a system behaves, benchmarking.",
        ("'Compare these two libraries', 'which is faster?', 'what does this service support?'.",
         "Many reads, searches and web fetches, few or no edits, ending in a finding.",
         "Runs a benchmark or probe to answer a question rather than to fix a defect."),
        "The main output is not facts gathered to inform a choice.",
        ("Studying a topic to understand it, with no decision ahead (learning).",
         "Judging the quality of results or work already made (review).",
         "Finding why one's own code or setup fails (debugging).",
         "Deciding the plan once the facts are known (planning)."),
    ),
    Question(
        "building", "Was this time mostly spent producing new code or content?",
        "The main output is something new: a feature, a command, a page, a document, an article, "
        "a script.",
        ("Many edits or writes and commits that add capability or content.",
         "Fixing review findings on the work being built.",
         "Pushing, merging and deploying the work, when nothing else is visible."),
        "The main output is not new code or content.",
        ("Finding the cause of a failure and fixing it (debugging).",
         "Upkeep of what exists: deps, config, environment, cleanup (maintenance).",
         "Judging or verifying work without changing it (review).",
         "A plan or spec as the deliverable (planning)."),
    ),
    Question(
        "debugging", "Was this time mostly spent finding why something is wrong in the work "
        "and fixing it?",
        "The work starts from a failure symptom (an error, a failing test or build, a wrong "
        "output) and goes after its cause.",
        ("'The test fails, find out why', 'why is this happening?' with a log or trace.",
         "Many runs and reads around one failure, with tool errors and a fix at the end.",
         "Explaining a wrong number or behaviour in one's own system."),
        "The work does not chase the cause of a failure.",
        ("Adding new capability, with small fixes along the way (building).",
         "A broken tool, connection, credential or dependency in the environment "
         "(maintenance).",
         "Judging whether results are good, with no failure to chase (review)."),
    ),
    Question(
        "review", "Was this time mostly spent judging work that already exists, one's own or "
        "others'?",
        "The main output is a judgment: review comments, a verdict on a PR, checking results or "
        "a document for errors or quality.",
        ("'Review this PR', 'check these four PRs', 'I added comments, check them'.",
         "Running review tools and reading their findings.",
         "Asking how well something scored, or whether results are accurate."),
        "The main output is not a judgment of existing work.",
        ("Making the changes a review asked for (building or debugging).",
         "Finding out facts about an outside system (research).",
         "Talking with people about the work (collaboration)."),
    ),
    Question(
        "learning", "Was this time mostly spent studying something to understand it, with no "
        "deliverable and no decision ahead?",
        "The developer wants to understand how something works for its own sake.",
        ("'Explain how this works', 'I want to understand this inside out'.",
         "Walking through a system, a paper or a video to understand it."),
        "The time has a deliverable or a decision ahead.",
        ("Gathering facts to make a choice (research).",
         "Asking the agent to explain its own output while other work goes on (the kind of "
         "that work).",
         "Finding why something fails (debugging)."),
    ),
    Question(
        "maintenance", "Was this time mostly spent on upkeep of what already exists?",
        "Keeping code, tools and environment working, with no new capability: dependencies, "
        "config, CI, connections, credentials, cleanup, docs refresh, packaging, checking that "
        "services are up.",
        ("'Bump the deps and fix lints', 'remove the merged branches', 'fix the tool connection'.",
         "Setting up or repairing a machine, VPN, plugin or access.",
         "Simplifying or refreshing existing docs; packaging files to share."),
        "The time adds capability or chases a defect in the work.",
        ("New features or content (building).",
         "Finding why the product code fails (debugging).",
         "Planning the upkeep rather than doing it (planning)."),
    ),
    Question(
        "collaboration", "Was this time mostly spent communicating with people?",
        "The main output is a message to people: a chat reply, an email, a ticket or comment "
        "for others, meeting notes, a status for a manager.",
        ("Drafting or rewriting a reply, 'give me a short reply', 'post it'.",
         "Writing tickets or comments for other people to act on.",
         "Reading people's messages and answering them."),
        "The main output is not a message to people.",
        ("Coordinating other agent sessions (the kind of work they do).",
         "Deciding the plan before a meeting (planning).",
         "Judging a colleague's PR (review)."),
    ),
    Question(
        "other", "Does this time fit none of the other kinds of work?",
        "No kind of work is visible, or it is not work.",
        ("Only session commands, logins or 'continue', with no visible work.",
         "Private matters with no work output."),
        "One of the other kinds of work is visible.",
        ("Checking that services are up (maintenance).",
         "Shipping steps only (building)."),
    ),
)

OUTCOME: tuple[Question, ...] = (
    Question(
        "ruled_out", "Was this work tried and then dropped on purpose?",
        "The developer or agent decided not to go on with it, for a stated reason.",
        ("'Let's drop this', 'not worth it', 'we won't do this'.",
         "The agent's last reply concludes the idea does not work and the developer agrees."),
        "The work was not dropped on purpose.",
        ("Waiting on someone or something outside (blocked).",
         "Answered or done (resolved).",
         "Left with a next step (still_open)."),
    ),
    Question(
        "blocked", "Is this work unable to go on until something outside it changes?",
        "The last exchange names a wait on a person, team, vendor, access, hardware or event "
        "outside the session.",
        ("'Waiting on the other team', 'needs admin access', 'after that PR merges'.",
         "The agent drafted a message to someone and the work waits on their answer."),
        "Nothing outside the work holds it up.",
        ("A next step the developer or agent can take now (still_open).",
         "Done, even if others may later act on it (resolved).",
         "An optional offer from the agent (resolved)."),
    ),
    Question(
        "still_open", "Is there visible evidence that this work will continue, with nothing "
        "outside holding it up?",
        "A step the work still needs is visible and can be taken now.",
        ("The agent asked for a decision it needs and got no answer.",
         "The developer's last request was not done in the agent's last reply.",
         "The last reply lists unfinished next steps or a run still going."),
        "No step the work needs is left.",
        ("A wait on someone outside (blocked).",
         "Only an optional extra offered ('want me to also ...?') (resolved).",
         "Dropped on purpose (ruled_out)."),
    ),
    Question(
        "resolved", "Was the question answered or the task done?",
        "The last exchange delivers the answer or result, and nothing the work needs is left.",
        ("The agent reports the result and the developer asks nothing more, or approves.",
         "Only an optional extra is offered.",
         "No visible request at all."),
        "Something the work needs is still left, or it was dropped.",
        ("An unanswered decision or a listed next step (still_open).",
         "A wait on someone outside (blocked).",
         "Dropped on purpose (ruled_out)."),
    ),
)

# The public groups: each leaf in exactly one group. The leaves stay in the database.
GROUPS: dict[str, dict[str, tuple[str, ...]]] = {
    "steer_intent": {"pushback": ("correction", "redirection", "stop"),
                     "directing": ("instruction", "framing"), "asking": ("clarification",),
                     "reviewing": ("review", "acceptance"), "other": ("other",)},
    "work_category": {"planning": ("planning",), "research": ("research", "learning"),
                      "building": ("building",), "debugging": ("debugging",),
                      "review": ("review", "collaboration"), "maintenance": ("maintenance",),
                      "other": ("other",)},
    "soft_outcome": {"closed": ("resolved", "ruled_out"), "open": ("still_open", "blocked")},
}

# A group of several leaves gets its own question, for the back-off when no leaf fires.
# `closed` is the outcome's rest: what is not open.
GROUP_QUESTIONS: dict[str, tuple[Question, ...]] = {
    "steer_intent": (
        Question(
            "pushback", "Does the message push back on what the agent is doing: say it is wrong, "
            "change its course, or stop it?",
            "The developer does not let the agent go on as it was: a mistake is named, the "
            "approach or goal is replaced, or the work is halted.",
            ("Says the agent's output or conduct is wrong (correction).",
             "Replaces the approach, plan or goal (redirection).",
             "Halts the work with no new direction (stop)."),
            "The message lets the agent's work go on as planned, or closes it happily.",
            ("A next step or go-ahead (directing).",
             "A question or an answer of fact (asking).",
             "A request to check work, or approval of it (reviewing)."),
        ),
        Question(
            "directing", "Does the message set the agent working: a new goal, or the next "
            "action in the current one?",
            "The developer starts new work or tells the agent what to do next.",
            ("Opens a new goal or topic with its background (framing).",
             "A go-ahead, a choice between the agent's options, or a next step (instruction)."),
            "The message does not set the agent working on something.",
            ("A question or an answer of fact (asking).",
             "A mistake named, a change of course or a halt (pushback).",
             "A request to check work, or approval of it (reviewing)."),
        ),
        Question(
            "reviewing", "Does the message judge work that exists: ask the agent to check it, "
            "or sign it off?",
            "The developer asks for a check or verdict on existing work, or approves finished "
            "work with nothing more asked.",
            ("Asks to review, verify or judge work (review).",
             "Approves finished work or reports it done (acceptance)."),
            "The message neither asks for a judgment nor signs work off.",
            ("A go-ahead or next step (directing).",
             "Saying the work is wrong (pushback).",
             "Asking what something means or the status (asking)."),
        ),
    ),
    "work_category": (
        Question(
            "research", "Was this time mostly spent finding out or studying, rather than "
            "producing, fixing or judging?",
            "The main output is knowledge: facts gathered to decide something, or understanding "
            "for its own sake.",
            ("Comparing options, reading docs, benchmarking to answer a question (research).",
             "Studying how something works, with no deliverable (learning)."),
            "The main output is not knowledge.",
            ("New code or content (building).", "A judgment of existing work (review).",
             "Chasing a failure (debugging)."),
        ),
        Question(
            "review", "Was this time mostly spent judging work or communicating about it with "
            "people?",
            "The main output is a judgment of existing work, or a message to people.",
            ("Review comments, verdicts, checks of results (review).",
             "Replies, emails, tickets or notes for people (collaboration)."),
            "The main output is neither a judgment nor a message to people.",
            ("Making the changes a review asked for (building).",
             "Coordinating agent sessions (the kind of work they do).",
             "Deciding the plan (planning)."),
        ),
    ),
    "soft_outcome": (
        Question(
            "open", "Is this work still open: will it continue, or does it wait on something?",
            "A step the work needs is left, whether it can be taken now or waits on something "
            "outside.",
            ("A listed next step, an unanswered decision, or an unfinished request (still_open).",
             "A named wait on a person, team, access or event (blocked)."),
            "Nothing the work needs is left.",
            ("Answered or done, maybe with an optional offer (resolved).",
             "Dropped on purpose (ruled_out)."),
        ),
    ),
}

QUESTIONS: dict[str, tuple[Question, ...]] = {
    "steer_intent": STEER, "work_category": WORK, "soft_outcome": OUTCOME}
CONTEXT: dict[str, tuple[str, tuple[str, ...]]] = {
    "steer_intent": (STEER_CONTEXT, STEER_RULES), "work_category": (WORK_CONTEXT, WORK_RULES),
    "soft_outcome": (OUTCOME_CONTEXT, OUTCOME_RULES)}


def noul(task: str, q: Question) -> dict[str, Any]:
    """The structured yes/no request for one question of `task`."""
    context, rules = CONTEXT[task]
    return {
        "version": f"aut-{task}-{q.label}-v{VER}", "question_id": f"is_{q.label}", "type": "noul",
        "instructions": {"task": q.task, "context": context, "evaluation_rules": [
            *rules, f"Return the probability that the answer is yes ({q.label})."]},
        "criteria": {
            "true": {"definition": q.definition,
                     "examples_of_qualifying_evidence": list(q.evidence)},
            "false": {"definition": q.not_definition, "do_not_count": list(q.do_not_count)}},
    }
