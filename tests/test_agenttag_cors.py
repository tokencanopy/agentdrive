import httpx
import pytest

from agentdrive.app import app

EXTENSION_ORIGIN = "chrome-extension://daapjbalkjaljhbafilpeafofdgkfbkf"
ALLOWED_METHODS = "GET, POST, PUT"
ALLOWED_HEADERS = "Authorization, Content-Type, If-Match, If-None-Match"


def _vary_values(response: httpx.Response) -> set[str]:
    return {
        value.strip().lower()
        for header in response.headers.get_list("vary")
        for value in header.split(",")
    }


@pytest.fixture
def transport() -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "null",
        "https://attacker.example",
    ],
)
async def test_other_origins_receive_no_cors_grant(transport, origin):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        preflight = await client.options(
            "/v1/agenttag/objects",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        actual = await client.get(
            "/v1/agenttag/objects",
            headers={"Origin": origin},
        )

    assert "access-control-allow-origin" not in preflight.headers
    assert "access-control-allow-methods" not in preflight.headers
    assert "access-control-allow-headers" not in preflight.headers
    assert "access-control-allow-origin" not in actual.headers


@pytest.mark.asyncio
async def test_cors_does_not_apply_outside_agenttag_routes(transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            "/health",
            headers={
                "Origin": EXTENSION_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )

    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-methods" not in response.headers
    assert "access-control-allow-headers" not in response.headers
