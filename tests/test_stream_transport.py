import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from examguard.config import Settings
from examguard.invocation import generate, streaming_transport, turn_ledger
from examguard.llm import OpenAICompatibleBackend
from examguard.schemas import LLMResult


def chunk(
    *, content=None, finish_reason=None, usage=None, reasoning=None, tool_calls=None
):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage, model="stream-model")


class FakeStream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.chunks)

    def close(self):
        self.closed = True


def backend_with_stream(stream):
    backend = OpenAICompatibleBackend.__new__(OpenAICompatibleBackend)
    backend.settings = Settings(api_key="test-key")
    backend.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=Mock(return_value=stream))
        )
    )
    return backend


class StreamingTransportTests(unittest.TestCase):
    def test_stream_collects_content_usage_and_closes(self):
        stream = FakeStream(
            [
                chunk(content="visible ", reasoning="hidden"),
                chunk(content="answer", tool_calls=[{"id": "ignored"}]),
                chunk(
                    finish_reason="stop",
                    usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
                ),
            ]
        )
        backend = backend_with_stream(stream)

        result = backend.generate_stream_with_options(
            [{"role": "user", "content": "question"}], max_output_tokens=10, timeout=2
        )

        self.assertEqual(result, LLMResult("visible answer", "stream-model", 3, 2))
        self.assertTrue(stream.closed)
        kwargs = backend.client.chat.completions.create.call_args.kwargs
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["stream_options"], {"include_usage": True})

    def test_stream_exception_closes_and_never_returns_partial_result(self):
        class BrokenStream(FakeStream):
            def __next__(self):
                if self.closed:
                    raise AssertionError("stream should have been closed")
                if not hasattr(self, "seen"):
                    self.seen = True
                    return chunk(content="partial")
                raise ValueError("transport failed")

        stream = BrokenStream([])
        backend = backend_with_stream(stream)
        with self.assertRaises(ValueError):
            backend.generate_stream_with_options([])
        self.assertTrue(stream.closed)

    def test_incomplete_stream_is_rejected_after_consumption(self):
        for finish_reason in (None, "length", "content_filter", "tool_calls"):
            with self.subTest(finish_reason=finish_reason):
                stream = FakeStream(
                    [chunk(content="partial", finish_reason=finish_reason)]
                )
                backend = backend_with_stream(stream)
                with self.assertRaises(RuntimeError):
                    backend.generate_stream_with_options([])
                self.assertTrue(stream.closed)

    def test_stream_deadline_and_accumulation_cap_are_enforced(self):
        stream = FakeStream(
            [chunk(content="123456789") for _ in range(2)]
            + [chunk(finish_reason="stop")]
        )
        backend = backend_with_stream(stream)
        with self.assertRaises(ValueError):
            backend.generate_stream_with_options([], max_output_tokens=1)
        self.assertTrue(stream.closed)

        stream = FakeStream([chunk(content="late"), chunk(finish_reason="stop")])
        backend = backend_with_stream(stream)
        with self.assertRaises(TimeoutError):
            backend.generate_stream_with_options([], timeout=-1)
        self.assertTrue(stream.closed)

    def test_streaming_is_opt_in_for_generation_and_never_classification_or_judge(self):
        class Backend:
            def __init__(self):
                self.stream_calls = 0
                self.normal_calls = 0

            def generate_stream_with_options(self, messages, **kwargs):
                self.stream_calls += 1
                return LLMResult("stream", "model", 1, 1)

            def generate_with_options(self, messages, **kwargs):
                self.normal_calls += 1
                return LLMResult("normal", "model", 1, 1)

        backend = Backend()
        with turn_ledger(Settings(max_calls_per_turn=3)) as ledger:
            with streaming_transport():
                self.assertEqual(generate(backend, [], "generation").text, "stream")
                self.assertEqual(generate(backend, [], "classification").text, "normal")
                self.assertEqual(generate(backend, [], "judge").text, "normal")
        self.assertEqual(backend.stream_calls, 1)
        self.assertEqual(backend.normal_calls, 2)
        self.assertEqual(len(ledger.calls), 3)


if __name__ == "__main__":
    unittest.main()
