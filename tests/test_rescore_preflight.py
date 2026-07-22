"""Startup connectivity preflight for rescore. fetch_available_models turns the model-list
call into a probe so a total outage aborts the batch fast instead of failing every job
through the SDK's retry/backoff; check_model_currency then reuses that same fetched list.
All hermetic — a fake client, no live API."""
import anthropic
import pytest

import rescore_viability as rv


class _Model:
    """Minimal stand-in for an SDK model object (only .id / .created_at are read)."""
    def __init__(self, id, created_at=0):
        self.id = id
        self.created_at = created_at


class _FakeClient:
    """Fake Anthropic client whose models.list() returns a list or raises `raises`."""
    def __init__(self, models=None, raises=None):
        self._models = models or []
        self._raises = raises
        self.models = self

    def list(self):
        if self._raises is not None:
            raise self._raises
        return list(self._models)


# ── fetch_available_models(): the connectivity probe ───────────────────────────
def test_returns_models_when_reachable():
    client = _FakeClient(models=[_Model("claude-haiku-4-5-20251001")])
    got = rv.fetch_available_models(client)
    assert [m.id for m in got] == ["claude-haiku-4-5-20251001"]


def test_returns_none_on_connection_error():
    """A connection/timeout failure (the outage case) → None, the signal to abort the run."""
    client = _FakeClient(raises=anthropic.APIConnectionError(message="down", request=None))
    assert rv.fetch_available_models(client) is None


def test_timeout_also_yields_none():
    """APITimeoutError subclasses APIConnectionError, so it's treated as unreachable too."""
    client = _FakeClient(raises=anthropic.APITimeoutError(request=None))
    assert rv.fetch_available_models(client) is None


def test_returns_empty_list_on_other_error():
    """A non-connectivity failure (e.g. auth/5xx) is 'reachable but unreadable' → [], so
    scoring still proceeds rather than aborting the whole batch."""
    client = _FakeClient(raises=RuntimeError("boom"))
    assert rv.fetch_available_models(client) == []


# ── check_model_currency(): now takes the pre-fetched list ─────────────────────
def test_currency_warns_when_model_unavailable(capsys):
    rv.check_model_currency([_Model("claude-haiku-4-5-20251001")], "claude-sonnet-5")
    assert "not available" in capsys.readouterr().err


def test_currency_silent_when_model_is_current(capsys):
    """Exact-or-prefix match to an available dated id and no newer sibling → no output."""
    rv.check_model_currency([_Model("claude-sonnet-5-20260101", created_at=1)], "claude-sonnet-5")
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_currency_notes_newer_sibling(capsys):
    """A newer dated id in the same family → an advisory note (not a warning)."""
    models = [_Model("claude-sonnet-5-20260101", created_at=1),
              _Model("claude-sonnet-5-20260601", created_at=2)]
    rv.check_model_currency(models, "claude-sonnet-5-20260101")
    assert "newer model is available" in capsys.readouterr().out


def test_currency_never_raises_on_bad_list():
    """The check stays non-fatal: a malformed list can't crash the run."""
    rv.check_model_currency([object()], "claude-sonnet-5")  # object() has no .id → guarded
