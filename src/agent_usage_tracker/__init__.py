"""agent-usage-tracker (aut): passive, subscription-safe tracker of coding-agent work.

Reads what each agent already writes to disk (no proxy, no API key), normalizes it into usage,
session, activity and active-time rows, and pushes them to a self-hosted sink. Commit outcomes and
local-model labels are separate pipelines over the same sink.
"""

__version__ = "0.1.0"
