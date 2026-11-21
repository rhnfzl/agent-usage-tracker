# First run

AUT reads the logs your coding agents already save. You can try it and check local coverage before
you choose a database or send any data.

## Start with a local report

```sh
aut setup
aut demo
aut setup --apply
aut summary
aut overview
```

`aut setup` previews the config path and checks local source metadata. Use `--json` for a
receipt with separate settings, source observations and delivery state. A scan receipt
describes locally emitted rows. The local queue can show pending rows, while delivery remains
unverified until you check the sink. A missing source can mean that the agent has yet to log
any work. Run `aut doctor --json` for the full private coverage report.

`aut setup --apply` creates the starter config only when the file is missing. The starter
uses local logs and leaves delivery off. The [config example](../config.example.toml) describes
optional sources and transport settings. The demo uses sample logs in a temporary directory.
It needs no provider account, database or previous setup.

## Schedule delivery to your own sink

Set the transport in your local config. The scheduled job needs access to any environment
variables that the config names. AUT leaves credentials out of scheduler entries. Use a stable
`aut` executable, such as one from `uv tool install agent-usage-tracker`.

```sh
aut setup --schedule
aut setup --apply --schedule
```

The first command previews. The second enables a per-user scan timer, which can start a scan
as soon as the scheduler loads it. It uses launchd on macOS, a systemd user timer on Linux,
and a scheduled task for the current interactive user on Windows. It keeps existing config and
refuses to replace a schedule created outside guided setup. On macOS the scheduled logs are
under `~/Library/Logs`. Linux uses the user journal and Windows uses Task Scheduler history.

The guided timer uses the default AUT config and state paths. For a custom setup, an existing
manual timer, or a system without a supported user scheduler, see
[the scheduler templates](../packaging/README.md). Use `aut summary` for local reporting on demand.

`aut setup --uninstall` removes only the scan entry created by guided setup. It keeps config,
scan state and logs. Custom daily jobs and manual scan timers have their own removal procedures.
