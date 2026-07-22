"""ingest.main() must survive a network failure per task by logging a clean error, not a
stacktrace. The trigger case is a power/internet outage: fetch_task_runs raises a
requests.ConnectionError (DNS/socket failure) *before* reaching the server, which is a
sibling of HTTPError under RequestException — the too-narrow original `except HTTPError`
let it escape. These stub the network boundary (the only Apify call reached), so they're
hermetic — no live requests."""
import sys

import pytest
import requests

import ingest


def _run_main_with_task_fetch_raising(exc, tmp_path, monkeypatch, capsys):
    """Drive ingest.main() (dry-run, so no write lock/DB mutation) with a single configured
    task whose run-fetch raises `exc`, and return captured (stdout, stderr)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'api_token = "x"\n'
        'username = "u"\n'
        f'db_path = "{tmp_path / "jobs.db"}"\n'
        "[[tasks]]\n"
        'name = "derek-career-site-generic"\n'
        'label = "test"\n'
    )
    # The first (and, because it raises, only) network call the loop makes per task.
    def _boom(*a, **k):
        raise exc
    monkeypatch.setattr(ingest, "fetch_task_runs", _boom)
    monkeypatch.setattr(sys, "argv", ["ingest.py", "--config", str(cfg), "--dry-run"])
    ingest.main()  # must NOT raise — the point of the fix
    return capsys.readouterr()


def test_connection_error_is_caught_not_raised(tmp_path, monkeypatch, capsys):
    """A DNS/socket failure (the outage case) is logged as one error line and doesn't crash
    the run — this is exactly what escaped the old `except HTTPError`."""
    err = requests.ConnectionError("nodename nor servname provided, or not known")
    out = _run_main_with_task_fetch_raising(err, tmp_path, monkeypatch, capsys)
    assert "ERROR fetching 'derek-career-site-generic'" in out.err
    # The run still reaches its normal end rather than aborting mid-loop.
    assert "Starting ingestion" in out.out


def test_http_error_still_caught(tmp_path, monkeypatch, capsys):
    """Broadening to RequestException must not lose the original HTTPError coverage."""
    out = _run_main_with_task_fetch_raising(
        requests.HTTPError("500 Server Error"), tmp_path, monkeypatch, capsys)
    assert "ERROR fetching 'derek-career-site-generic'" in out.err


def test_timeout_is_caught(tmp_path, monkeypatch, capsys):
    """A read/connect timeout (also a RequestException, also not an HTTPError) is caught too."""
    out = _run_main_with_task_fetch_raising(
        requests.Timeout("timed out"), tmp_path, monkeypatch, capsys)
    assert "ERROR fetching 'derek-career-site-generic'" in out.err
