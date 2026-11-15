import json

import httpx
import pytest

from cloud_agent.agent import AgentError, CodingAgent, ModelOutputTruncated
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


def completion(finish="stop", tokens=100, calls=None, content=None):
    delta = {}
    if calls:
        delta["tool_calls"] = calls
    if content:
        delta["content"] = content
    events = [{"choices": [{"delta": delta, "finish_reason": finish}]}]
    if tokens is not None:
        events.append({"choices": [], "usage": {"total_tokens": tokens}})
    return events


def provider(monkeypatch, replies):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= len(replies), "Unexpected extra model request"
        return httpx.Response(200, text=sse(replies[len(requests) - 1]))

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "cloud_agent.agent.httpx.AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    return requests


async def test_length_finish_retains_actual_usage_and_flushes_text():
    recorded = []
    requests = []

    async def emit(kind, data, tokens):
        recorded.append((kind, data, tokens))

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=sse(completion("length", 73, content="short tail")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ModelOutputTruncated) as error:
            await CodingAgent(Settings(llm_max_output_tokens=512))._completion(
                client, [], 10_000, emit
            )
    assert error.value.used_tokens == 73 and error.value.maximum == 512
    assert requests[0]["max_tokens"] == 512
    assert recorded == [
        ("text_delta", {"text": "short tail"}, 0),
        (
            "usage",
            {"tokens": 73, "estimated": False, "finish_reason": "length", "max_tokens": 512},
            73,
        ),
    ]


@pytest.mark.parametrize("reported", [None, 0, -1, True, "100", 100_000])
async def test_missing_or_invalid_usage_is_marked_estimated(reported):
    recorded = []

    async def emit(kind, data, tokens):
        recorded.append((kind, data, tokens))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=sse(completion(tokens=reported)))
        )
    ) as client:
        _, tokens = await CodingAgent(Settings())._completion(client, [], 10_000, emit)
    assert 0 < tokens <= 10_000
    assert len(recorded) == 1 and recorded[0][1]["estimated"] is True


async def test_recovery_discards_all_truncated_calls_and_counts_retry_usage(monkeypatch):
    discarded = [
        {
            "index": 0,
            "id": "discard-0",
            "function": {"name": "execute", "arguments": '{"command":"bad"}'},
        },
        {
            "index": 1,
            "id": "discard-1",
            "function": {"name": "write_file", "arguments": '{"path":'},
        },
    ]
    good = [
        {
            "index": 0,
            "id": "good",
            "function": {"name": "write_file", "arguments": '{"path":"ok.py","content":"ok"}'},
        }
    ]
    requests = provider(
        monkeypatch,
        [
            completion("length", 70, discarded),
            completion("tool_calls", 80, good),
            completion(tokens=90, content="done"),
        ],
    )
    writes, recorded = [], []

    class Sandbox:
        async def execute(self, *args):
            pytest.fail("No command from a truncated response may execute")

        async def write_file(self, path, content):
            writes.append((path, content))
            return {"exit_code": 0, "output": "written"}

    async def emit(kind, data, tokens):
        recorded.append((kind, data, tokens))

    result = await CodingAgent(Settings(llm_api_key="test-key")).run(
        "task", Sandbox(), emit, 24_000
    )
    assert result.summary == "done" and result.used_tokens == 240
    assert sum(tokens for _, _, tokens in recorded) == 240
    assert writes == [("ok.py", "ok")]
    assert len([kind for kind, _, _ in recorded if kind == "model_retry"]) == 1
    assert "None of its tool calls were executed" in requests[1]["messages"][-1]["content"]
    assert "discard-" not in json.dumps(requests[1:])


async def test_truncation_recovery_has_a_hard_retry_limit(monkeypatch):
    requests = provider(monkeypatch, [completion("length"), completion("length")])
    recorded = []

    async def emit(kind, data, tokens):
        recorded.append((kind, data, tokens))

    with pytest.raises(AgentError, match="recovery limit exhausted"):
        await CodingAgent(Settings(llm_api_key="test-key", llm_max_truncation_retries=1)).run(
            "task", object(), emit, 24_000
        )
    assert len(requests) == 2
    assert sum(tokens for _, _, tokens in recorded) == 200
    assert len([kind for kind, _, _ in recorded if kind == "model_retry"]) == 1


async def test_recovery_cannot_exceed_task_budget(monkeypatch):
    requests = provider(monkeypatch, [completion("length", tokens=None)])
    recorded = []

    async def emit(kind, data, tokens):
        recorded.append((kind, data, tokens))

    with pytest.raises(AgentError, match="budget"):
        await CodingAgent(Settings(llm_api_key="test-key", llm_max_output_tokens=65_536)).run(
            "task", object(), emit, 10_000
        )
    assert len(requests) == 1
    assert sum(tokens for _, _, tokens in recorded) == 10_000


@pytest.mark.parametrize(
    "finish", [None, "content_filter", "aborted", "insufficient_system_resource"]
)
async def test_non_length_failures_do_not_retry_and_preserve_usage(monkeypatch, finish):
    requests = provider(monkeypatch, [completion(finish)])
    recorded = []

    async def emit(kind, data, tokens):
        recorded.append((kind, data, tokens))

    with pytest.raises(AgentError):
        await CodingAgent(Settings(llm_api_key="test-key")).run("task", object(), emit, 24_000)
    assert len(requests) == 1
    assert sum(tokens for _, _, tokens in recorded) == 100
    assert not any(kind == "model_retry" for kind, _, _ in recorded)


async def test_transport_interruption_is_charged_once_without_exposing_provider_data():
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError("provider-private-information")

    recorded = []

    async def emit(kind, data, tokens):
        recorded.append((kind, data, tokens))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Interrupted()))
    ) as client:
        with pytest.raises(AgentError, match="transport interrupted") as error:
            await CodingAgent(Settings())._completion(client, [], 10_000, emit)
    assert "provider-private-information" not in str(error.value)
    assert len(recorded) == 1 and recorded[0][0] == "usage"
    assert recorded[0][1]["estimated"] and recorded[0][1]["interrupted"]


async def test_http_rejection_does_not_charge_usage():
    async def emit(*args):
        pytest.fail("Rejected requests must not be charged")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(429, text="private details"))
    ) as client:
        with pytest.raises(AgentError, match="HTTP 429"):
            await CodingAgent(Settings())._completion(client, [], 10_000, emit)
