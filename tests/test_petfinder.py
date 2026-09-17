"""The HTTP client is mocked end to end — no network, no API key needed."""

import httpx
import pytest

from furrster.petfinder import PetfinderClient, PetfinderError
from tests.conftest import fixture


def make_client(handler) -> PetfinderClient:
    transport = httpx.MockTransport(handler)
    return PetfinderClient(
        "k", "s", client=httpx.Client(transport=transport), sleep_between_pages=0
    )


def test_pagination_walks_every_page():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=fixture("token.json"))
        page = int(request.url.params.get("page", 1))
        calls.append(page)
        return httpx.Response(200, json=fixture(f"animals_page{page}.json"))

    animals = list(make_client(handler).iter_animals(max_pages=10))
    assert calls == [1, 2]
    assert len(animals) == 33
    assert animals[0]["name"] == "Bruno"


def test_max_pages_is_respected():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=fixture("token.json"))
        return httpx.Response(200, json=fixture("animals_page1.json"))

    animals = list(make_client(handler).iter_animals(max_pages=1))
    assert len(animals) == 20


def test_token_is_reused_across_requests():
    tokens = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            tokens.append(1)
            return httpx.Response(200, json=fixture("token.json"))
        return httpx.Response(200, json=fixture("animals_page2.json"))

    client = make_client(handler)
    list(client.iter_animals(max_pages=1))
    list(client.iter_animals(max_pages=1))
    assert sum(tokens) == 1


def test_401_refreshes_token_then_succeeds():
    state = {"first": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=fixture("token.json"))
        if state["first"]:
            state["first"] = False
            return httpx.Response(401, json={})
        return httpx.Response(200, json=fixture("animals_page2.json"))

    animals = list(make_client(handler).iter_animals(max_pages=1))
    assert len(animals) == 13


def test_400_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json=fixture("token.json"))
        return httpx.Response(400, text="ERR-00002 invalid parameters")

    with pytest.raises(PetfinderError):
        list(make_client(handler).iter_animals(max_pages=1))
