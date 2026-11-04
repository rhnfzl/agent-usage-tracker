"""Names the config check and the CLI parser need, with no imports: label.tasks loads every
collector, and neither of them should. tests/test_label.py keeps these equal to tasks.TASKS.
"""

DECISION_TASKS = ("work_category", "steer_intent", "soft_outcome", "aw_title_relevance")
NOTE = "daily_note"          # generative, private: one draft per day (run.py), not a decision
TASK_NAMES = (*DECISION_TASKS, NOTE)
DAYS = 90                    # the window units are rebuilt over (teach, review, eval, train)
