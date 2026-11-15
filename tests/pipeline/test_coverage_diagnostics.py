"""Private collection diagnostics distinguish parse loss from an empty pass."""

from collections import Counter


def test_skipped_records_reach_only_the_current_collector(capsys):
    from agent_usage_tracker import diagnostics
    from agent_usage_tracker.collectors._util import warn_skipped

    with diagnostics.capture() as observed:
        warn_skipped("fixture", Counter({"malformed": 2, "activity:KeyError": 3, "missing_payloads": 4}))
    assert observed.skipped == {"malformed_records": 2, "activity_errors": 3,
                                "missing_payloads": 4}
    warning = capsys.readouterr().err
    assert "malformed record(s)" in warning
    assert "4 record(s) have no stored payload" in warning
    with diagnostics.capture() as later:
        pass
    assert not later.skipped


def test_nested_capture_restores_the_outer_collector():
    from agent_usage_tracker import diagnostics

    with diagnostics.capture() as outer:
        diagnostics.observe_client("cli")
        with diagnostics.capture() as inner:
            diagnostics.observe_client("t3-code")
        diagnostics.observe_client("sdk-py")
    assert outer.clients == {"cli", "sdk-py"}
    assert inner.clients == {"t3-code"}


def test_unknown_client_labels_are_hashed_and_bounded():
    from agent_usage_tracker import diagnostics

    secret = "private project content"
    with diagnostics.capture() as observed:
        diagnostics.observe_client(secret)
        diagnostics.observe_client(secret)
        for index in range(100):
            diagnostics.observe_client(f"unlisted-client-{index}")
    assert secret not in str(observed.clients)
    assert len(observed.clients) <= 33
    assert observed.client_overflow
    assert any(label.startswith("unknown:") for label in observed.clients)


def test_codex_tui_is_named_metadata_without_promoting_origin():
    from agent_usage_tracker import diagnostics
    from agent_usage_tracker.collectors.codex import run_origin

    with diagnostics.capture() as observed:
        diagnostics.observe_client("codex-tui")
    assert observed.clients == {"codex-tui"}
    assert observed.skipped == {}
    for source, expected in [
        ("cli", "interactive"), ({"subagent": {}}, "subagent"), ("future-source", "unknown"),
    ]:
        assert run_origin({"originator": "codex-tui", "source": source}) == expected
        assert run_origin({"originator": "future-client", "source": source}) == expected


def test_diagnostics_outside_a_scan_do_not_leak_into_the_next_scan():
    from agent_usage_tracker import diagnostics

    diagnostics.observe_client("cli")
    diagnostics.note_skips(Counter({"malformed": 7, "arbitrary secret field": 4}))
    with diagnostics.capture() as observed:
        diagnostics.observe_client({"untrusted": "not a label"})
    assert not observed.clients
    assert not observed.skipped
