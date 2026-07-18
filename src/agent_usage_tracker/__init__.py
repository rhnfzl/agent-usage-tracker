"""agent-usage-tracker (aut): passive, subscription-safe coding-agent token tracker.

Reads what each agent already writes to disk (no proxy, no API key), normalizes
every assistant turn into one UsageEvent, and pushes to a self-hosted sink.
"""

__version__ = "0.1.0"
