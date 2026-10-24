import unittest
from types import SimpleNamespace
from unittest import mock
from unittest import mock

from algosage.config import Settings
from algosage.invocation import generate
from algosage.llm import OpenAICompatibleBackend
from algosage.schemas import LLMResult
from algosage.service import normalize_model_params


class ModelSettingsTests(unittest.TestCase):
    def test_model_params_are_clamped_to_provider_limits(self):
        params = normalize_model_params(
            {
                "temperature": 9,
                "top_p": -2,
                "frequency_penalty": 10,
                "presence_penalty": -1,
                "max_output_tokens": 100000,
                "enter_behavior": "evil",
                "font_size": "huge",
                "autoscroll": 0,
                "keep_drafts": 1,
                "animations": "no",
                "audit_summary": "yes",
            },
            Settings(max_output_tokens=2048),
        )
        self.assertEqual(params["temperature"], 2.0)
        self.assertEqual(params["top_p"], 0.0)
        self.assertEqual(params["frequency_penalty"], 2.0)
        self.assertEqual(params["presence_penalty"], 0.0)
        self.assertEqual(params["max_output_tokens"], 2048)
        self.assertEqual(params["enter_behavior"], "enter")
        self.assertEqual(params["font_size"], "standard")
        self.assertFalse(params["autoscroll"])
        self.assertTrue(params["keep_drafts"])
        self.assertFalse(params["animations"])
        self.assertTrue(params["audit_summary"])

    def test_generation_sampling_is_sent_to_openai_compatible_backend(self):
        backend = OpenAICompatibleBackend.__new__(OpenAICompatibleBackend)
        backend.settings = Settings(api_key="test-key", extra_body={"enable_thinking": False})
        backend.client = completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            model="model",
        )
        backend.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=unittest.mock.Mock(return_value=completion))
            )
        )
        sampling = {
            "temperature": 0.25,
            "top_p": 0.8,
            "frequency_penalty": 0.4,
            "presence_penalty": 0.1,
        }
        backend.generate_with_options([], sampling=sampling)
        kwargs = backend.client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["temperature"], 0.25)
        self.assertEqual(kwargs["top_p"], 0.8)
        self.assertEqual(kwargs["frequency_penalty"], 0.4)
        self.assertEqual(kwargs["presence_penalty"], 0.1)
        self.assertEqual(kwargs["extra_body"], {"enable_thinking": False})

    def test_generation_options_reach_pipeline_backend(self):
        from algosage.pipeline import AlgoSageTutor
        from algosage.schemas import TutorRequest

        class Backend:
            def __init__(self):
                self.options = None

            def generate_with_options(self, messages, **kwargs):
                self.options = kwargs
                return LLMResult("answer", "model", 1, 1)

        backend = Backend()
        tutor = AlgoSageTutor(backend, Settings(max_output_tokens=64, output_guard="rules"))
        tutor.respond(TutorRequest(question="解释复杂度"), generation_options={"temperature": 0.2})
        self.assertEqual(backend.options["sampling"]["temperature"], 0.2)


if __name__ == "__main__":
    unittest.main()
