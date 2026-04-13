import pytest
import jwt as _jwt
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient
from fastapi.responses import RedirectResponse

import core.key_manager as _km
from services.server_registry import all_client_ids

_KERNEL_SERVER_IDS = all_client_ids()


def _google_registry():
    return {
        "google": {
            "label": "Google",
            "type": "oidc",
            "redirect_uri": "http://localhost:8081/auth/callback",
        }
    }


@pytest.mark.asyncio
@patch.dict("routers.auth_router.REGISTERED_PROVIDERS", _google_registry(), clear=True)
@patch("routers.auth_router.find_mcp_client_by_client_id", return_value=["http://localhost:3000/callback"])
@patch("routers.auth_router.oauth.create_client")
async def test_authorize_redirect(mock_create_client, _mock_find, client: AsyncClient):
    mock_create_client.return_value.authorize_redirect = AsyncMock(
        return_value=RedirectResponse(url="https://accounts.google.com/o/oauth2/v2/auth")
    )
    resp = await client.get(
        "/auth/authorize",
        params={
            "response_type": "code",
            "client_id": "client-123",
            "redirect_uri": "http://localhost:3000/callback",
        },
    )
    assert resp.status_code == 307
    assert "accounts.google.com" in resp.headers.get("location", "")


@pytest.mark.asyncio
@patch.dict("routers.auth_router.REGISTERED_PROVIDERS", _google_registry(), clear=True)
@patch("routers.auth_router.find_mcp_client_by_client_id", return_value=["http://localhost:3000/callback"])
@patch("routers.auth_router.oauth.create_client")
async def test_authorize_success(mock_create_client, _mock_find, client: AsyncClient):
    mock_create_client.return_value.authorize_redirect = AsyncMock(
        return_value=RedirectResponse(url="https://accounts.google.com/o/oauth2/v2/auth")
    )
    resp = await client.get(
        "/auth/authorize",
        params={
            "response_type": "code",
            "client_id": "client-123",
            "redirect_uri": "http://localhost:3000/callback",
            "scope": "read",
            "state": "abc",
            "code_challenge": "a" * 43,
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 307
    assert "accounts.google.com" in resp.headers["location"]


@pytest.mark.asyncio
async def test_authorize_invalid_response_type(client: AsyncClient):
    resp = await client.get(
        "/auth/authorize",
        params={
            "response_type": "token",
            "client_id": "client-123",
            "redirect_uri": "http://localhost:3000/callback",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("Unsupported response_type")


@pytest.mark.asyncio
@patch("routers.auth_router.find_mcp_client_by_client_id", return_value=["http://localhost:3000/callback"])
async def test_authorize_invalid_redirect(_mock_find, client: AsyncClient):
    """Third-party client: redirect_uri not in the artifact's registered list."""
    resp = await client.get(
        "/auth/authorize",
        params={
            "response_type": "code",
            "client_id": "client-123",
            "redirect_uri": "http://evil.com/cb",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "redirect_uri not registered for this client"


@pytest.mark.asyncio
async def test_list_providers(client: AsyncClient):
    resp = await client.get("/auth/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert "providers" in body
    assert "password" in body


@pytest.mark.asyncio
async def test_token_missing_params_auth_code_flow(client: AsyncClient):
    # Missing required form fields should return 400
    resp = await client.post("/auth/token", data={"grant_type": "authorization_code"})
    assert resp.status_code == 400
    assert "Missing one or more required parameters" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_token_missing_refresh_token(client: AsyncClient):
    resp = await client.post("/auth/token", data={"grant_type": "refresh_token"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Missing required parameter: refresh_token"


@pytest.mark.asyncio
@patch("core.config.PASSWORD_AUTH_ENABLED", True)
@patch("routers.auth_router.create_jwt_token", return_value="jwt")
@patch("routers.auth_router.verify_password", return_value=True)
@patch("services.person_service.get_user_by_email")
async def test_password_login_success(mock_get_user_by_email, _, __, client: AsyncClient):
    from entities.person import Person

    mock_get_user_by_email.return_value = Person(
        id="user-123",
        email="test@example.com",
        name="Test User",
        picture=None,
        password_hash="pbkdf2_sha256$1$aa$bb",
    )

    resp = await client.post(
        "/auth/password/login",
        json={"identifier": "test@example.com", "password": "pw"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body.get("token_type") == "bearer"


@pytest.mark.asyncio
@patch("core.config.PASSWORD_AUTH_ENABLED", True)
@patch("routers.auth_router.create_jwt_token", return_value="jwt")
@patch("routers.auth_router.verify_password", return_value=False)
@patch("services.person_service.get_user_by_email")
async def test_password_login_invalid_credentials(mock_get_user_by_email, _, __, client: AsyncClient):
    from entities.person import Person

    mock_get_user_by_email.return_value = Person(
        id="user-123",
        email="test@example.com",
        name="Test User",
        picture=None,
        password_hash="pbkdf2_sha256$1$aa$bb",
    )

    resp = await client.post(
        "/auth/password/login",
        json={"identifier": "test@example.com", "password": "bad"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
@patch("core.config.PASSWORD_AUTH_ENABLED", True)
@patch("routers.auth_router.create_jwt_token", return_value="jwt")
@patch("services.person_service.create_user_with_password")
async def test_password_register_success(mock_create_user, _, client: AsyncClient):
    from entities.person import Person

    mock_create_user.return_value = Person(
        id="user-123",
        email="new@example.com",
        name="New User",
        picture=None,
    )
    resp = await client.post(
        "/auth/password/register",
        json={"username": "newuser", "email": "new@example.com", "password": "password1234", "name": "New User"},
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.asyncio
@patch("core.config.PASSWORD_AUTH_ENABLED", True)
@patch("services.person_service.create_user_with_password", side_effect=ValueError("Email already registered"))
async def test_password_register_does_not_enumerate_existing_email(_, client: AsyncClient):
    resp = await client.post(
        "/auth/password/register",
        json={"username": "existing", "email": "existing@example.com", "password": "password1234", "name": "User"},
    )
    assert resp.status_code == 400
    assert resp.json().get("detail") == "Registration failed"


@pytest.mark.asyncio
@patch(
    "routers.auth_router.get_jwks",
    return_value={"keys": [{"kty": "RSA", "kid": "test-key-1", "n": "abc", "e": "AQAB"}]},
)
async def test_jwks_endpoint_returns_public_key(_, client: AsyncClient):
    """GET /.well-known/jwks.json returns a valid JWKS document."""
    resp = await client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "keys" in data
    assert isinstance(data["keys"], list)
    assert len(data["keys"]) >= 1
    # Each key must have at minimum kty and kid fields
    key = data["keys"][0]
    assert "kty" in key
    assert "kid" in key


# ---------------------------------------------------------------------------
# Kernel server client_credentials short-circuit
#
# Servers in server_registry.all_client_ids() authenticate with the shared
# PLATFORM_INTERNAL_SECRET and never touch the ServerCredential table. These
# tests lock down the fast-path against secret-mismatch, missing-secret, and
# DB-bypass regressions, and verify the issued JWT carries the right shape.
# ---------------------------------------------------------------------------

def _decode(token: str) -> dict:
    return _jwt.decode(token, _km.get_public_key_pem(), algorithms=["RS256"], audience="agience")


@pytest.mark.asyncio
@patch("core.config.PLATFORM_INTERNAL_SECRET", "shared-secret")
async def test_kernel_client_credentials_success(client: AsyncClient):
    client_id = sorted(_KERNEL_SERVER_IDS)[0]
    resp = await client.post(
        "/auth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": "shared-secret",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    claims = _decode(body["access_token"])
    assert claims["sub"] == f"server/{client_id}"
    assert claims["principal_type"] == "server"
    assert claims["client_id"] == client_id
    assert claims["aud"] == "agience"
    # Kernel servers come up with full wildcard scopes — no DB-stored intersection.
    assert "tool:*:invoke" in claims["scopes"]


@pytest.mark.asyncio
@patch("core.config.PLATFORM_INTERNAL_SECRET", "shared-secret")
async def test_kernel_client_credentials_wrong_secret(client: AsyncClient):
    client_id = sorted(_KERNEL_SERVER_IDS)[0]
    resp = await client.post(
        "/auth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": "WRONG",
        },
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"


@pytest.mark.asyncio
@patch("core.config.PLATFORM_INTERNAL_SECRET", "")
async def test_kernel_client_credentials_unconfigured_returns_503(client: AsyncClient):
    client_id = sorted(_KERNEL_SERVER_IDS)[0]
    resp = await client.post(
        "/auth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": "anything",
        },
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "server_error"


@pytest.mark.asyncio
@patch("core.config.PLATFORM_INTERNAL_SECRET", "shared-secret")
async def test_kernel_client_credentials_does_not_query_db(client: AsyncClient):
    """Regression: kernel servers must NEVER fall through to db_get_server_credential.
    If they did, the kernel fast-path would be defeated and seeded ServerCredentials
    would be required for every persona — which is exactly what the kernel path exists
    to avoid. Patching the DB lookup to raise lets us assert it was never called."""
    client_id = sorted(_KERNEL_SERVER_IDS)[0]
    with patch(
        "routers.auth_router.db_get_server_credential",
        side_effect=AssertionError("kernel path must not hit the DB"),
    ):
        resp = await client.post(
            "/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": "shared-secret",
            },
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("client_id", sorted(_KERNEL_SERVER_IDS))
@patch("core.config.PLATFORM_INTERNAL_SECRET", "shared-secret")
async def test_every_kernel_server_id_can_obtain_a_token(client_id, client: AsyncClient):
    """Each persona's client_id must mint a valid kernel JWT. If a server is added to
    the manifest this test will pick it up automatically."""
    resp = await client.post(
        "/auth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": "shared-secret",
        },
    )
    assert resp.status_code == 200, f"{client_id} failed: {resp.text}"
    claims = _decode(resp.json()["access_token"])
    assert claims["sub"] == f"server/{client_id}"


@pytest.mark.asyncio
@patch("core.config.PLATFORM_INTERNAL_SECRET", "shared-secret")
async def test_third_party_client_id_falls_through_to_db(client: AsyncClient):
    """A non-kernel client_id must NOT be accepted with PLATFORM_INTERNAL_SECRET —
    it must go through the ServerCredential DB path."""
    with patch("routers.auth_router.db_get_server_credential", return_value=None):
        resp = await client.post(
            "/auth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "third-party-server",
                "client_secret": "shared-secret",
            },
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_token_endpoint_rejects_unsupported_grant_type(client: AsyncClient):
    resp = await client.post("/auth/token", data={"grant_type": "implicit"})
    assert resp.status_code == 400
    assert "Unsupported grant_type" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_client_credentials_missing_id_or_secret(client: AsyncClient):
    resp = await client.post(
        "/auth/token",
        data={"grant_type": "client_credentials", "client_id": "x"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Delegation tokens (RFC 8693 act.sub) — issued by Core, verified by servers
# ---------------------------------------------------------------------------

class TestDelegationTokens:
    def test_issue_delegation_token_carries_act_sub(self):
        from services.auth_service import issue_delegation_token

        token = issue_delegation_token("agience-server-nexus", "user-42")
        claims = _jwt.decode(
            token,
            _km.get_public_key_pem(),
            algorithms=["RS256"],
            audience="agience-server-nexus",
        )
        assert claims["sub"] == "user-42"
        assert claims["aud"] == "agience-server-nexus"
        assert claims["act"] == {"sub": "agience-server-nexus"}
        assert claims["principal_type"] == "delegation"
        # exp is in the near future
        assert claims["exp"] > claims["iat"]
        # Default TTL is 300s
        assert claims["exp"] - claims["iat"] <= 301

    def test_delegation_token_audience_mismatch_rejected(self):
        from services.auth_service import issue_delegation_token

        token = issue_delegation_token("agience-server-aria", "user-1")
        with pytest.raises(_jwt.InvalidAudienceError):
            _jwt.decode(
                token,
                _km.get_public_key_pem(),
                algorithms=["RS256"],
                audience="agience-server-nexus",
            )


# ---------------------------------------------------------------------------
# is_client_redirect_allowed matrix
# ---------------------------------------------------------------------------

class TestRedirectAllowed:
    def test_loopback_127_any_port_allowed(self):
        from services.auth_service import is_client_redirect_allowed

        assert is_client_redirect_allowed("http://127.0.0.1:54321/cb")
        assert is_client_redirect_allowed("http://localhost:9999/cb")

    def test_https_loopback_not_special_cased(self):
        from services.auth_service import is_client_redirect_allowed

        # https://localhost is unusual; only allowed if it matches a configured base.
        with patch("core.config.FRONTEND_URI", "http://example.com"), patch(
            "core.config.BACKEND_URI", "http://example.com"
        ):
            assert not is_client_redirect_allowed("https://localhost:8443/cb")

    def test_vscode_dev_always_allowed(self):
        from services.auth_service import is_client_redirect_allowed

        assert is_client_redirect_allowed("https://vscode.dev/some/path")

    def test_unknown_https_origin_rejected(self):
        from services.auth_service import is_client_redirect_allowed

        with patch("core.config.FRONTEND_URI", "http://example.com"), patch(
            "core.config.BACKEND_URI", "http://example.com"
        ):
            assert not is_client_redirect_allowed("https://evil.example/cb")

    def test_non_http_scheme_rejected(self):
        from services.auth_service import is_client_redirect_allowed

        assert not is_client_redirect_allowed("javascript:alert(1)")
        assert not is_client_redirect_allowed("file:///etc/passwd")

    def test_garbage_input_rejected(self):
        from services.auth_service import is_client_redirect_allowed

        assert not is_client_redirect_allowed("")
        assert not is_client_redirect_allowed("not a url")


@pytest.mark.asyncio
async def test_openid_configuration_endpoint(client: AsyncClient):
    """GET /.well-known/openid-configuration returns discovery document."""
    resp = await client.get("/.well-known/openid-configuration")
    assert resp.status_code == 200
    data = resp.json()
    assert "issuer" in data
    assert "jwks_uri" in data
    assert "token_endpoint" in data
    assert data["jwks_uri"].endswith("/.well-known/jwks.json")
