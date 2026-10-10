from tests.fixtures.composer_fakes import DelayedComposerFake
from tests.helpers.composer_operations import message_body, settle_sync


def test_shared_route_fixture_settles_a_real_worker_turn_in_one_loop(test_client) -> None:
    composer = DelayedComposerFake()
    composer.release.set()
    test_client.app.state.composer_service = composer
    session = test_client.post("/api/sessions", json={"title": "Ordinary worker turn"}).json()
    settled = settle_sync(
        test_client,
        test_client.app,
        path=f"/api/sessions/{session['id']}/messages",
        body=message_body("Build a pipeline", state_id=None),
    )
    assert settled.accepted.status_code == 202
    assert settled.result()["message"]["content"] == "Completed delayed response."
    assert composer.calls == 1
