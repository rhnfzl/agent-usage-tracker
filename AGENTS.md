# Verification

Use the matching project console pytest entrypoint, as CI does. Do not substitute `python -m pytest`: it adds the checkout root to `sys.path` and can hide import collection failures. Keep the required test targets unchanged.

Use `./scripts/local-ci.sh check` for full local validation and for the batch publisher's `--full` command.
The full local preflight must finish within 60 seconds, including cleanup. Fix performance regressions without dropping checks or retrying slow runs to obtain a pass.
GitHub Actions still runs the required cross-platform checks before merge.
