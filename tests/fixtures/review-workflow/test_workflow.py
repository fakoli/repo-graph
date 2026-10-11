"""Synthetic pytest-shaped review cases; source only, never run by this corpus."""

import pytest

from workflow import (VoicePool, discover_commands, drain_cancelled, mcp_record,
                      normalize_tags, recover_manifest, require_scope,
                      require_token, rest_record, safe_relative_path, send_tts,
                      serve_http, summarize_usage)


def test_cli_discovery_hides_experimental_command():
    registry = {"status": {"public": True}, "review": {"public": False}}
    assert discover_commands(registry) == ["status"]


@pytest.mark.parametrize("scope", [None, "all"])
def test_cli_refuses_missing_or_unbounded_scope(scope):
    with pytest.raises(ValueError):
        require_scope(scope)


def test_http_response_closes_on_success():
    closed = []
    assert serve_http("ok", lambda: closed.append(True))["status"] == 200
    assert closed == [True]


def test_http_response_closes_when_body_is_unhelpful():
    closed = []
    assert serve_http(None, lambda: closed.append(True))["body"] is None
    assert closed == [True]


def test_tts_transport_preserves_transport_failure():
    class FailedTransport:
        def send(self, audio):
            return {"status": 503}

    with pytest.raises(RuntimeError):
        send_tts(FailedTransport(), b"audio")


def test_voice_pool_does_not_reissue_a_lease():
    pool = VoicePool(["voice-a"])
    assert pool.acquire() == "voice-a"
    with pytest.raises(RuntimeError):
        pool.acquire()


def test_rest_and_mcp_share_record_contract():
    class Core:
        def record(self, payload):
            return {"id": payload["id"], "status": "recorded"}

    core = Core()
    assert rest_record(core, {"id": "case-1"}) == mcp_record(core, {"id": "case-1"})


def test_usage_keeps_owner_grant_and_token_directions():
    usage = summarize_usage("operator-1", "grant-1", 3, 5)
    assert usage == {"owner": "operator-1", "grant": "grant-1",
                     "input_tokens": 3, "output_tokens": 5}


def test_shared_legacy_usage_is_not_a_person():
    usage = summarize_usage("_legacy", None, 3, 5)
    assert usage["owner_kind"] == "shared_legacy"
    assert "owner" not in usage


def test_authentication_rejects_missing_token():
    with pytest.raises(PermissionError):
        require_token(None, "fixture-token")


def test_cancellation_requires_a_complete_drain():
    with pytest.raises(RuntimeError):
        drain_cancelled(["result"])


def test_recovery_uses_last_complete_manifest():
    recovered = recover_manifest({"complete": False}, {"complete": True, "id": "prior"})
    assert recovered == {"state": "recovered", "manifest": {"complete": True, "id": "prior"}}


@pytest.mark.parametrize("path", ["/outside", "../outside"])
def test_filesystem_containment_rejects_escaping_path(path):
    with pytest.raises(ValueError):
        safe_relative_path(path)


def test_duplicate_tag_order_is_a_safe_consolidation_candidate():
    assert normalize_tags(["voice", "router", "voice"]) == ("router", "voice")


def test_duplicate_tag_order_with_different_input_is_same_contract():
    assert normalize_tags(["router", "voice"]) == ("router", "voice")
