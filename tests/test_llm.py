import pytest

from furrster.llm import LLMError, parse_json


def test_parses_bare_json():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_parses_fenced_json():
    assert parse_json('Sure!\n```json\n{"a": 1}\n```\nHope that helps.') == {"a": 1}


def test_parses_json_with_surrounding_prose():
    assert parse_json('Here you go: {"a": [1,2]} done') == {"a": [1, 2]}


def test_raises_on_garbage():
    with pytest.raises(LLMError):
        parse_json("no json here at all")
