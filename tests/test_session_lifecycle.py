"""Offline regression tests for trusted identity and durable safety budgets."""

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

from examguard.config import Settings
from examguard.llm import MockChatBackend
from examguard.service import TutorService


def service(**settings):
    return TutorService(
        Settings(mode="mock", **settings),
        backend=MockChatBackend(),
        allow_policy_override=False,
    )


class LifecycleTests(unittest.TestCase):
    def test_reconnect_same_session_reuses_budget_and_context(self):
        svc = service()
        ctx = svc.new_context("a")
        svc.chat(ctx, "解释 KMP 前缀函数", question_id="q")
        snapshot = ctx.session_guard.snapshot()
        self.assertIs(svc.new_context("a"), ctx)
        self.assertEqual(svc.get_context("a").session_guard.snapshot(), snapshot)

    def test_drop_clears_history_without_resetting_budget(self):
        svc = service()
        ctx = svc.new_context("a")
        svc.chat(ctx, "解释 KMP 前缀函数", question_id="q")
        before = ctx.session_guard.snapshot()
        svc.drop_context("a")
        self.assertEqual(ctx.history, [])
        fresh = svc.new_context("a")
        self.assertEqual(fresh.history, [])
        self.assertEqual(fresh.session_guard.snapshot(), before)

    def test_trusted_principal_shares_budget_across_sessions(self):
        svc = service()
        a = svc.new_context("a", principal_id="student-1")
        svc.chat(a, "解释 KMP 前缀函数", question_id="q")
        b = svc.new_context("b", principal_id="student-1")
        self.assertIs(a.session_guard, b.session_guard)
        self.assertEqual(b.history, [])
        other = svc.new_context("c", principal_id="student-2")
        self.assertEqual(other.session_guard.snapshot(), {})

    def test_same_session_cannot_rebind_principal(self):
        svc = service()
        svc.new_context("a", principal_id="student-1")
        with self.assertRaises(PermissionError):
            svc.new_context("a", principal_id="student-2")

    def test_anonymous_new_sessions_have_no_trusted_cross_chat_identity(self):
        svc = service()
        a, b = svc.new_context("a"), svc.new_context("b")
        self.assertIsNot(a.session_guard, b.session_guard)

    def test_lru_eviction_preserves_budget(self):
        with mock.patch("examguard.service.MAX_LIVE_SESSIONS", 1):
            svc = service()
            a = svc.new_context("a")
            svc.chat(a, "解释 KMP 前缀函数", question_id="q")
            before = a.session_guard.snapshot()
            svc.new_context("b")
            self.assertTrue(a.history)
            self.assertEqual(svc.new_context("a").session_guard.snapshot(), before)

    def test_budget_capacity_rejects_new_principal_but_keeps_existing(self):
        with mock.patch("examguard.service.MAX_SECURITY_BUDGETS", 1):
            svc = service()
            a = svc.new_context("a")
            svc.drop_context("a")
            with self.assertRaises(RuntimeError):
                svc.new_context("b")
            self.assertIs(svc.new_context("a").session_guard, a.session_guard)

    def test_reset_requires_backend_permission_and_teacher_reset_is_explicit(self):
        svc = service()
        ctx = svc.new_context("a")
        svc.chat(ctx, "解释 KMP 前缀函数", question_id="q")
        before = ctx.session_guard.snapshot()
        with self.assertRaises(PermissionError):
            svc.reset_security_state(ctx)
        self.assertEqual(ctx.session_guard.snapshot(), before)
        svc.allow_policy_override = True
        svc.reset_security_state(ctx)
        self.assertEqual(ctx.session_guard.snapshot(), {})
        self.assertEqual(ctx.history, [])

    def test_server_question_binding_rejects_switch_before_backend(self):
        svc = service(question_id="server-q")
        ctx = svc.new_context("a")
        svc.chat(ctx, "解释 KMP 前缀函数")
        self.assertEqual(set(ctx.session_guard.snapshot()), {"qid:server-q"})
        with self.assertRaises(ValueError):
            svc.chat(ctx, "解释 KMP 前缀函数", question_id="other")
        self.assertEqual(len(ctx.history), 2)

    def test_complete_turn_serializes_same_principal(self):
        svc = service()
        a = svc.new_context("a", principal_id="student")
        b = svc.new_context("b", principal_id="student")
        entered, release, second_entered = (threading.Event() for _ in range(3))
        observed = []
        original = svc._respond

        def probe(ctx, *args):
            if ctx is a:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test timed out")
            else:
                second_entered.set()
                observed.append(
                    sum(s["turns"] for s in ctx.session_guard.snapshot().values())
                )
                observed.append(len(a.history))
            return original(ctx, *args)

        svc._respond = probe
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(svc.chat, a, "解释 KMP 前缀函数", "", "q")
            self.assertTrue(entered.wait(2))
            second = pool.submit(svc.chat, b, "解释 KMP 前缀函数", "", "q")
            try:
                self.assertFalse(
                    second_entered.wait(0.1),
                    "second turn entered before first committed",
                )
            finally:
                release.set()
            first.result(timeout=3)
            second.result(timeout=3)
        self.assertEqual(observed, [1, 2])
        self.assertEqual(a.session_guard.snapshot()["qid:q"]["turns"], 2)


class PersistenceTests(unittest.TestCase):
    def test_sqlite_survives_restart_and_isolates_exams_and_principals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.sqlite3")
            first = service(state_path=path, exam_id="exam-a")
            a = first.new_context("a", principal_id="student")
            first.chat(a, "解释 KMP 前缀函数", question_id="q")
            before = a.session_guard.snapshot()
            second = service(state_path=path, exam_id="exam-a")
            b = second.new_context("b", principal_id="student")
            self.assertEqual(b.session_guard.snapshot(), before)
            self.assertEqual(b.history, [])
            self.assertEqual(
                second.new_context("c", principal_id="other").session_guard.snapshot(),
                {},
            )
            third = service(state_path=path, exam_id="exam-b")
            self.assertEqual(
                third.new_context("d", principal_id="student").session_guard.snapshot(),
                {},
            )
            second.chat(b, "解释 KMP 前缀函数", question_id="q")
            first.chat(a, "解释 KMP 前缀函数", question_id="q")
            self.assertEqual(a.session_guard.snapshot()["qid:q"]["turns"], 3)

    def test_sqlite_serializes_separate_service_instances_for_one_principal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.sqlite3")
            first, second = service(state_path=path), service(state_path=path)
            a = first.new_context("a", principal_id="student")
            b = second.new_context("b", principal_id="student")
            entered, release, second_entered = (threading.Event() for _ in range(3))
            first_respond, second_respond = first._respond, second._respond
            observed = []

            def blocked(*args):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test timed out")
                return first_respond(*args)

            def observe(ctx, *args):
                second_entered.set()
                observed.append(ctx.session_guard.snapshot()["qid:q"]["turns"])
                return second_respond(ctx, *args)

            first._respond, second._respond = blocked, observe
            with ThreadPoolExecutor(max_workers=2) as pool:
                one = pool.submit(first.chat, a, "解释 KMP 前缀函数", "", "q")
                self.assertTrue(entered.wait(2))
                two = pool.submit(second.chat, b, "解释 KMP 前缀函数", "", "q")
                try:
                    self.assertFalse(second_entered.wait(0.1))
                finally:
                    release.set()
                one.result(timeout=3)
                two.result(timeout=3)
            self.assertEqual(observed, [1])
            self.assertEqual(b.session_guard.snapshot()["qid:q"]["turns"], 2)

    def test_invalid_input_does_not_poison_persistent_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = service(state_path=str(Path(tmp) / "state.sqlite3"))
            ctx = svc.new_context("a")
            with self.assertRaises(ValueError):
                svc.chat(ctx, "")
            self.assertTrue(svc.chat(ctx, "解释 KMP 前缀函数").answer)

    def test_sqlite_capacity_not_bypassed_by_restart(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("examguard.service.MAX_SECURITY_BUDGETS", 1),
        ):
            path = str(Path(tmp) / "state.sqlite3")
            service(state_path=path).new_context("a", principal_id="one")
            reopened = service(state_path=path)
            with self.assertRaises(RuntimeError):
                reopened.new_context("b", principal_id="two")
            reopened.new_context("c", principal_id="one")

    def test_teacher_reset_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.sqlite3")
            svc = service(state_path=path)
            ctx = svc.new_context("a", principal_id="student")
            svc.chat(ctx, "解释 KMP 前缀函数", question_id="q")
            svc.allow_policy_override = True
            svc.reset_security_state(ctx)
            self.assertEqual(
                service(state_path=path)
                .new_context("b", principal_id="student")
                .session_guard.snapshot(),
                {},
            )


class SettingsBoundaryTests(unittest.TestCase):
    def test_offline_defaults_and_runtime_state_path(self):
        self.assertEqual(Settings().state_path, "")
        with (
            mock.patch("examguard.config._load_dotenv"),
            mock.patch.dict("os.environ", {}, clear=True),
        ):
            cfg = Settings.from_env()
        self.assertEqual(cfg.state_path, ".examguard/state.sqlite3")
        self.assertEqual(cfg.exam_id, "demo")
        self.assertEqual(cfg.question_id, "")

    def test_env_fields(self):
        env = {
            "EG_QUESTION_ID": "q1",
            "EG_EXAM_ID": "exam1",
            "EG_STATE_FILE": "",
            "EG_MAX_CALLS_PER_TURN": "4",
            "EG_TURN_TIMEOUT_SECONDS": "12.5",
            "EG_CLASSIFIER_MAX_OUTPUT_TOKENS": "100",
            "EG_JUDGE_MAX_OUTPUT_TOKENS": "120",
            "EG_MIN_CLASSIFIER_CONFIDENCE": ".8",
        }
        with (
            mock.patch("examguard.config._load_dotenv"),
            mock.patch.dict("os.environ", env, clear=True),
        ):
            cfg = Settings.from_env()
        self.assertEqual(
            (cfg.question_id, cfg.exam_id, cfg.state_path), ("q1", "exam1", "")
        )
        self.assertEqual((cfg.max_calls_per_turn, cfg.turn_timeout), (4, 12.5))
        self.assertEqual(
            (cfg.classifier_max_output_tokens, cfg.judge_max_output_tokens), (100, 120)
        )
        self.assertEqual(cfg.min_classifier_confidence, 0.8)

    def test_invalid_ranges_fail_closed(self):
        for field, value in (
            ("max_calls_per_turn", 0),
            ("turn_timeout", float("nan")),
            ("turn_timeout", 0),
            ("classifier_max_output_tokens", -1),
            ("judge_max_output_tokens", 0),
            ("min_classifier_confidence", 1.1),
            ("min_classifier_confidence", float("nan")),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                replace(Settings(), **{field: value})
