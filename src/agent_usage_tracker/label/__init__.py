"""`aut label`: a local model labels units of work on this machine (optional extra `aut[label]`).

One interface for every task (core.Request -> backend distribution -> core.Response), one stored
shape (an agents.activity_event row of kind `label`: an enum value, a probability, versions;
never text). The text a decision reads (steer text, window titles) comes from this machine's own
logs at run time, is redacted, and is never stored or logged.

  core      Request / Response, temperature scaling, per-label tau, the label row
  backends  mlx_letter (an instruct model read through answer-letter logits) and the encoder slot
  tasks     the closed candidate lists, questions and unit builders per task
  run       `aut label run` / `aut label backfill`: watermark, time budget, delivery, heartbeat
  evalset   the silver, gold and eval files under core.data_dir() / "eval", the shared unit
            order and the once-only day split
  teach     `aut label teach`: silver labels (local model always, Sonnet for Claude Code units)
  gold      `aut label review`: the gold page on 127.0.0.1
  bakeoff   `aut label eval` / `choose` / `train-encoder`: metrics, the winner into label.json
"""
