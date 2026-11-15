import json

import httpx
import pytest

from cloud_agent.agent import AgentError, CodingAgent
from cloud_agent.config import Settings


def sse(events):
    return "\n\n".join("data: " + json.dumps(event) for event in events) + "\n\ndata: [DONE]\n\n"


async def test_streaming_tool_fragments_and_usage():
    events = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call1",
                                "function": {"name": "read_", "arguments": '{"path":'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"name": "file", "arguments": '"/workspace"}'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"total_tokens": 100}},
    ]
    recorded = []

    async def emit(kind, data, tokens):
        recorded.append((kind, data, tokens))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=sse(events)))
    ) as client:
        message, tokens = await CodingAgent(Settings())._completion(client, [], 10_000, emit)
    assert tokens == 100
    assert message["tool_calls"][0]["function"] == {
        "name": "read_file",
        "arguments": '{"path":"/workspace"}',
    }
    assert recorded[-1][2] == 100


async def test_incomplete_stream_is_charged_conservatively():
    recorded = []

    async def emit(kind, data, tokens):
        recorded.append((kind, data, tokens))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=sse([])))
    ) as client:
        with pytest.raises(AgentError, match="complete"):
            await CodingAgent(Settings())._completion(client, [], 10_000, emit)
    assert recorded[-1][0] == "usage" and 0 < recorded[-1][2] <= 10_000


async def test_rejects_request_outside_budget():
    async def emit(*args):
        raise AssertionError("Must not emit or send a request")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("must not request"))
    ) as client:
        with pytest.raises(AgentError, match="budget"):
            await CodingAgent(Settings())._completion(client, [], 100, emit)


async def test_unknown_tool_is_returned_to_model(monkeypatch):
    agent = CodingAgent(Settings(llm_api_key="test-key"))
    replies = iter(
        [
            (
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "x", "function": {"name": "host_shell", "arguments": "{}"}}
                    ],
                },
                10,
            ),
            ({"role": "assistant", "content": "done"}, 10),
        ]
    )

    async def completion(client, messages, remaining, emit):
        return next(replies)

    monkeypatch.setattr(agent, "_completion", completion)
    recorded = []

    async def emit(kind, data, tokens):
        recorded.append((kind, data))

    result = await agent.run("task", object(), emit, 24_000)
    assert result.summary == "done"
    assert any(kind == "tool_result" and data["exit_code"] == 1 for kind, data in recorded)
