"""`aut label`: a local model labels units of work on this machine (optional extra `aut[label]`).

One interface for every task (core.Request -> backend distribution -> core.Response), one stored
shape (an agents.activity_event row of kind `label`: an enum value, a probability, versions;
never text). The text a decision reads (steer text, window titles) comes from this machine's own
logs at run time, is redacted, and is never stored or logged.

  core      Request / Response, temperature and Platt scaling, per-label tau, the label row
  backends  the model table by short name and one backend per protocol: mlx_letter (an instruct
            model read through answer-letter logits), mlx_elephant, mlx_card, mlx_pplx, mlx_clef,
            llama_server (a decision GGUF behind llama-server), and the encoder slot
  clef      Clef's joint schema reader on MLX, imported only when clef-flash runs
  merge     `python -m agent_usage_tracker.label.merge NAME`: fold a LoRA into its base once
  tasks     the closed candidate lists, questions and unit builders per task
  run       `aut label run` / `aut label backfill`: watermark, time budget, delivery, heartbeat
  evalset   the silver, gold, eval and score files under core.data_dir() / "eval", the shared
            unit order, the five folds and the final test days
  teach     `aut label teach`: silver labels (local model always, Sonnet for Claude Code units)
  gold      `aut label review`: the gold page on 127.0.0.1
  bakeoff   `aut label eval` / `choose` / `train-encoder`: saved scores, out-of-fold metrics,
            the table per task, the owner's pick into label.json
  ensemble  `aut label ensemble`: odd combinations of models, from saved scores only
"""
