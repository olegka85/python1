import json
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from garment_erp.api.app import create_app
from garment_erp.infrastructure.database import Database
from garment_erp.infrastructure.security import principal_from_claims
from garment_erp.settings import Settings
from tests.conftest import TOKEN, command, material, style


async def test_authentication_role_permissions_and_fresh_authorization_on_replay(harness):
    client = harness.client
    assert (
        await client.get("/materials", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
    cotton = await material(client, stock="1")
    body = {"quantity": "2", "reference": "receipt-2"}
    path = f"/materials/{cotton['id']}/receipts"
    await command(client, path, body, "same-key")
    harness.app.state.settings = harness.app.state.settings.model_copy(
        update={"dev_role": "viewer"}
    )
    assert (await client.get("/materials")).status_code == 200
    await command(client, path, body, "same-key", expected=403)
    await command(
        client, "/materials", {"sku": "denied", "name": "denied", "unit": "m"}, expected=403
    )
    assert (await client.get("/audit")).status_code == 403


async def test_tenant_isolation_for_reads_foreign_keys_and_same_sku(harness):
    client = harness.client
    cotton = await material(client)
    tee = await style(client, cotton["id"])
    settings = Settings(
        _env_file=None, environment="test", auth_mode="dev", dev_token=TOKEN, dev_tenant="factory-b"
    )
    other = create_app(settings, harness.db)
    async with (
        other.router.lifespan_context(other),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=other),
            base_url="http://other",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as other_client,
    ):
        assert (await other_client.get(f"/materials/{cotton['id']}")).status_code == 404
        assert (await other_client.get(f"/styles/{tee['id']}")).status_code == 404
        assert (await other_client.get("/materials")).json()["items"] == []
        await command(
            other_client,
            "/styles",
            {
                "sku": "bad",
                "name": "bad",
                "bom": [
                    {"material_id": cotton["id"], "size": "M", "color": "black", "quantity": "1"}
                ],
            },
            expected=404,
        )
        other_material = await material(other_client)
        assert other_material["id"] != cotton["id"]
        assert len((await other_client.get("/audit")).json()["items"]) == 1


def test_zitadel_role_claims_are_bound_to_organization():
    claims = {
        "sub": "user",
        "urn:zitadel:iam:user:resourceowner:id": "factory-a",
        "urn:zitadel:iam:org:project:roles": [
            {"admin": {"factory-b": "b"}},
            {"viewer": {"factory-a": "a"}},
        ],
    }
    assert principal_from_claims(claims, "project").roles == ("viewer",)
    with pytest.raises(HTTPException):
        principal_from_claims({**claims, "urn:zitadel:iam:org:project:roles": ["admin"]}, "project")


@pytest.mark.parametrize(
    "fault,expected",
    [
        (None, 200),
        ("aud", 401),
        ("iss", 401),
        ("exp", 401),
        ("signature", 401),
        ("organization", 403),
    ],
)
async def test_signed_zitadel_jwt_validation(harness, fault, expected):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    timestamp = int(time.time())
    claims = {
        "sub": "alice",
        "iss": "https://id.example",
        "aud": "project",
        "iat": timestamp,
        "exp": timestamp + 300,
        "urn:zitadel:iam:user:resourceowner:id": "factory-a",
        "urn:zitadel:iam:org:project:roles": {"planner": {"factory-a": "factory"}},
    }
    if fault in {"aud", "iss"}:
        claims[fault] = "wrong"
    if fault == "exp":
        claims["exp"] = timestamp - 60
    if fault == "organization":
        claims["urn:zitadel:iam:org:project:roles"] = {"admin": {"factory-b": "other"}}
    token = jwt.encode(claims, wrong_key if fault == "signature" else private, algorithm="RS256")
    harness.app.state.settings = harness.app.state.settings.model_copy(
        update={
            "auth_mode": "zitadel",
            "zitadel_issuer": "https://id.example",
            "zitadel_audience": "project",
            "cerbos_url": "http://cerbos",
        }
    )
    harness.app.state.jwk_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private.public_key())
    )

    async def check(request):
        body = json.loads(request.content)
        assert body["principal"]["roles"] == ["planner"]
        assert body["principal"]["attr"]["tenant_id"] == "factory-a"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "resource": {"kind": "factory", "id": "factory-a"},
                        "actions": {"read": "EFFECT_ALLOW"},
                    }
                ]
            },
        )

    await harness.app.state.http.aclose()
    harness.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(check))
    response = await harness.client.get("/materials", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == expected, response.text


@pytest.mark.parametrize(
    "decision,expected",
    [
        ("EFFECT_ALLOW", 200),
        ("EFFECT_DENY", 403),
        ("unavailable", 503),
        ("malformed", 503),
        ("wrong_resource", 503),
    ],
)
async def test_cerbos_decisions_and_unavailability_fail_closed(harness, decision, expected):
    harness.app.state.settings = harness.app.state.settings.model_copy(
        update={"cerbos_url": "http://cerbos"}
    )

    async def check(request):
        assert request.url.path == "/api/check/resources"
        if decision == "unavailable":
            raise httpx.ConnectError("offline")
        if decision == "malformed":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "resource": {
                            "kind": "factory",
                            "id": "factory-b" if decision == "wrong_resource" else "factory-a",
                        },
                        "actions": {"read": decision},
                    }
                ]
            },
        )

    await harness.app.state.http.aclose()
    harness.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(check))
    response = await harness.client.get("/materials")
    assert response.status_code == expected


def test_production_rejects_development_authentication():
    with pytest.raises(ValueError, match="Production requires Zitadel and Cerbos"):
        Settings(_env_file=None, environment="production", auth_mode="dev")


async def test_readiness_reports_missing_schema_without_creating_it(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/empty.db")
    app = create_app(Settings(_env_file=None, environment="test", auth_mode="dev"), db)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 503
    finally:
        await db.close()
