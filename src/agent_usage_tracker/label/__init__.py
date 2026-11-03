"""`aut label`: a local model labels units of work on this machine (optional extra `aut[label]`).

One interface for every task (core.Request -> backend distribution -> core.Response), one stored
shape (an agents.activity_event row of kind `label`: an enum value, a probability, versions;
never text). The text a decision reads (steer text, window titles) comes from this machine's own
logs at run time, is redacted, and is never stored or logged.

  core      Request / Response, temperature scaling, per-label tau, the label row
  backends  mlx_letter (an instruct model read through answer-letter logits) and the encoder slot
  tasks     the closed candidate lists, questions and unit builders per task
  run       `aut label run` / `aut label backfill`: watermark, time budget, delivery, heartbeat

Seams for what comes next: a teacher (`aut label teach`) and the gold page (`aut label review`)
build core.Request through tasks.units() and write under core.data_dir() / "eval"; the bake-off
(`aut label eval` / `choose`) uses core.fit_temperature / core.fit_tau and writes the winner per
task to core.choice_path(), which run.py reads; `train-encoder` fills backends.Encoder.
"""
