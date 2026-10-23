"""Tests for ``GET /v1/shims``: registry report, idle window, and auth gating."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.shims import register_shim, registered_shims, reset_for_tests, shim_fired
from omnigent.server.auth import AuthProvider
from omnigent.server.routes import shims as shims_route
from omnigent.server.routes.shims import create_shims_router


class _HeaderIdentity(AuthProvider):
    """Resolve the user from an ``x-test-user`` header, or ``None`` when absent."""

    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Give every test an empty registry, a private firing log, and no production shims.

    The route imports the production modules that declare shims; pinning the
    list empty keeps the report to the shims each test registers.

    :param tmp_path: Pytest temporary directory for the data dir.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: Iterator yielding once, with the registry cleared on both sides.
    """
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(shims_route, "_SHIM_DECLARING_MODULES", ())
    production_shims = registered_shims()
    reset_for_tests()
    yield
    reset_for_tests()
    # Production modules register on import only once, so put theirs back.
    for shim in production_shims:
        register_shim(**asdict(shim))


class _AdminList:
    """Permission-store stand-in whose admins are a fixed set of user ids."""

    def __init__(self, admins: set[str]) -> None:
        self._admins = admins

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._admins


def _client(
    auth_provider: AuthProvider | None = None,
    permission_store: _AdminList | None = None,
) -> TestClient:
    """
    Mount the shims router on a bare app.

    :param auth_provider: Auth provider to gate the route with, or ``None``.
    :returns: A test client for the app.
    """
    app = FastAPI()
    app.include_router(
        create_shims_router(
            auth_provider=auth_provider,
            permission_store=permission_store,  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    return TestClient(app)


def test_get_shims_reports_registered_shims_with_firings() -> None:
    """The report lists each registered shim with counts and the idle window."""
    quiet = register_shim(
        "test.quiet",
        reason="Never fires.",
        model_gap="None observed.",
        added_in="0.19.0",
        review_by="0.24.0",
        owner="tests",
    )
    busy = register_shim(
        "test.busy",
        reason="Fires a lot.",
        model_gap="A gap.",
        added_in="0.19.0",
        review_by="0.24.0",
        owner="tests",
    )
    shim_fired(busy, model="gpt-4o", harness="openai-agents")
    shim_fired(busy, model="gpt-4o")

    response = _client().get("/v1/shims")

    assert response.status_code == 200
    body = response.json()
    assert body["idle_days"] == 30
    by_name = {entry["name"]: entry for entry in body["shims"]}
    assert set(by_name) == {quiet.name, busy.name}
    assert by_name["test.quiet"]["fire_count"] == 0
    assert by_name["test.quiet"]["last_fired_at"] is None
    # Just registered on this deployment: not retirable until a full window passes.
    assert by_name["test.quiet"]["retirable"] is False
    assert by_name["test.quiet"]["first_seen_at"].endswith("+00:00")
    assert by_name["test.busy"] == {
        "name": "test.busy",
        "reason": "Fires a lot.",
        "model_gap": "A gap.",
        "added_in": "0.19.0",
        "review_by": "0.24.0",
        "owner": "tests",
        "fire_count": 2,
        "last_fired_at": by_name["test.busy"]["last_fired_at"],
        "by_model": {"gpt-4o": 2},
        "retirable": False,
        "first_seen_at": by_name["test.busy"]["first_seen_at"],
    }
    assert by_name["test.busy"]["last_fired_at"].endswith("+00:00")


def test_get_shims_honours_idle_days_and_rejects_out_of_range() -> None:
    """``idle_days`` is echoed back and bounded to 1..3650."""
    client = _client()
    assert client.get("/v1/shims", params={"idle_days": 7}).json()["idle_days"] == 7
    assert client.get("/v1/shims", params={"idle_days": 0}).status_code == 422
    assert client.get("/v1/shims", params={"idle_days": 3651}).status_code == 422


def test_get_shims_loads_declaring_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Shims declared by a module the server never imported still appear.

    A throwaway module registers on import, mirroring how the runtime and runner
    modules declare theirs; the route must import it before snapshotting.
    """
    module_name = "shim_route_test_declaring_module"
    (tmp_path / f"{module_name}.py").write_text(
        "from omnigent.runtime.shims import register_shim\n"
        "register_shim('test.declared_on_import', reason='r', model_gap='g',"
        " added_in='0.19.0', review_by='0.24.0', owner='tests')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(shims_route, "_SHIM_DECLARING_MODULES", (module_name,))
    try:
        names = {entry["name"] for entry in _client().get("/v1/shims").json()["shims"]}
    finally:
        sys.modules.pop(module_name, None)
    assert names == {"test.declared_on_import"}


def test_get_shims_requires_user_in_multi_user_mode() -> None:
    """Without an identity the route fails closed; with one it serves the report."""
    client = _client(_HeaderIdentity())
    # The bare test app has no OmnigentError handler, so the 401 surfaces as the
    # exception the server would render.
    with pytest.raises(OmnigentError) as unauthorized:
        client.get("/v1/shims")
    assert unauthorized.value.code == ErrorCode.UNAUTHORIZED
    assert client.get("/v1/shims", headers={"x-test-user": "alice"}).status_code == 200


def test_multi_user_report_is_admin_only() -> None:
    """Per-model counts span every workspace, so only admins read them."""
    client = _client(_HeaderIdentity(), _AdminList({"admin"}))
    assert client.get("/v1/shims", headers={"x-test-user": "admin"}).status_code == 200
    # The bare test app has no OmnigentError handler; the 403 surfaces as the exception.
    with pytest.raises(OmnigentError) as forbidden:
        client.get("/v1/shims", headers={"x-test-user": "member"})
    assert forbidden.value.code == ErrorCode.FORBIDDEN
    assert "read the shim registry" in str(forbidden.value)
