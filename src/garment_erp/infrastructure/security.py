import asyncio
import hmac
from dataclasses import dataclass
from typing import Annotated

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import PyJWKClientConnectionError

ROLE_ACTIONS = {
    "admin": {
        "read",
        "catalog:write",
        "inventory:receive",
        "orders:write",
        "production:write",
        "purchases:write",
        "operations:read",
    },
    "planner": {"read", "catalog:write", "orders:write", "production:write"},
    "warehouse": {"read", "inventory:receive"},
    "purchaser": {"read", "purchases:write", "inventory:receive"},
    "viewer": {"read"},
}
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    id: str
    tenant_id: str
    roles: tuple[str, ...]


def principal_from_claims(claims: dict, audience: str) -> Principal:
    tenant = claims.get("urn:zitadel:iam:user:resourceowner:id")
    subject = claims.get("sub")
    if (
        not isinstance(tenant, str)
        or not 1 <= len(tenant) <= 150
        or not isinstance(subject, str)
        or not 1 <= len(subject) <= 150
    ):
        raise HTTPException(401, "Missing identity/organization claims")
    role_map = claims.get(
        f"urn:zitadel:iam:org:project:{audience}:roles",
        claims.get("urn:zitadel:iam:org:project:roles", {}),
    )
    if isinstance(role_map, list):
        merged = {}
        for entry in role_map:
            if not isinstance(entry, dict):
                raise HTTPException(401, "Invalid project role claims")
            for role, organizations in entry.items():
                if not isinstance(organizations, dict):
                    raise HTTPException(401, "Invalid project role claims")
                merged.setdefault(role, {}).update(organizations)
        role_map = merged
    if not isinstance(role_map, dict):
        raise HTTPException(401, "Invalid project role claims")
    roles = tuple(
        role
        for role, organizations in role_map.items()
        if role in ROLE_ACTIONS and isinstance(organizations, dict) and tenant in organizations
    )
    if not roles:
        raise HTTPException(403, "No factory project roles for this organization")
    return Principal(subject, tenant, roles)


async def authenticate(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    settings = request.app.state.settings
    if settings.auth_mode == "dev":
        expected = settings.dev_token.get_secret_value()
        if not expected or not hmac.compare_digest(
            expected.encode(), credentials.credentials.encode()
        ):
            raise HTTPException(401, "Invalid development token")
        return Principal("local-demo", settings.dev_tenant, (settings.dev_role,))
    client = request.app.state.jwk_client
    if client is None or not settings.zitadel_issuer or not settings.zitadel_audience:
        raise HTTPException(503, "Identity provider is not configured")
    if len(credentials.credentials) > 8192:
        raise HTTPException(401, "Invalid access token")
    try:
        key = await asyncio.to_thread(client.get_signing_key_from_jwt, credentials.credentials)
        claims = jwt.decode(
            credentials.credentials,
            key.key,
            algorithms=["RS256"],
            audience=settings.zitadel_audience,
            issuer=settings.zitadel_issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            leeway=10,
        )
    except PyJWKClientConnectionError as exc:
        raise HTTPException(503, "Identity provider unavailable") from exc
    except (jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(
            401, "Invalid access token", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    return principal_from_claims(claims, settings.zitadel_audience)


async def authorize(request: Request, principal: Principal, action: str) -> None:
    settings = request.app.state.settings
    if settings.cerbos_url:
        body = {
            "requestId": request.state.request_id,
            "principal": {
                "id": principal.id,
                "roles": list(principal.roles),
                "attr": {"tenant_id": principal.tenant_id},
            },
            "resources": [
                {
                    "resource": {
                        "kind": "factory",
                        "id": principal.tenant_id,
                        "attr": {"tenant_id": principal.tenant_id},
                    },
                    "actions": [action],
                }
            ],
        }
        try:
            response = await request.app.state.http.post(
                settings.cerbos_url.rstrip("/") + "/api/check/resources", json=body
            )
            response.raise_for_status()
            result = response.json()["results"][0]
            if (
                result["resource"]["id"] != principal.tenant_id
                or result["resource"]["kind"] != "factory"
            ):
                raise ValueError("Unexpected authorization resource")
            allowed = result["actions"].get(action) == "EFFECT_ALLOW"
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise HTTPException(503, "Authorization provider unavailable") from exc
    elif settings.auth_mode == "dev" and settings.environment != "production":
        allowed = any(action in ROLE_ACTIONS.get(role, set()) for role in principal.roles)
    else:
        raise HTTPException(503, "Authorization provider is not configured")
    if not allowed:
        raise HTTPException(403, "Action is not allowed")
