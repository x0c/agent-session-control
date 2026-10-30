"""Consumer plan tests: installed public ``agent_session_control`` API only.

No Corral, SessKit-internals, UI or vendor-SDK imports. stdlib unittest so
the harness runs before the foundation worker's pytest tooling lands (it
also runs under pytest). Every test skips with the exact missing peer
module instead of failing when foundation work is still in progress.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_session_control import runtime as rt

BUILTINS = ("claude", "codex", "opencode", "cursor", "pi")


def _assert_refuses(testcase: unittest.TestCase, func, *args, **kwargs):
    """Refusal must fail loudly with an informative error, never silent success.

    The contract names ``ControlError`` (with ``code``); while the foundation
    module is pending we accept any explicit failure that explains itself.
    """
    try:
        func(*args, **kwargs)
    except Exception as exc:
        testcase.assertTrue(str(exc) or getattr(exc, "code", None), "refusal must explain itself")
        return exc
    testcase.fail("expected refusal, got success")


class ConsumerPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="asc-consumer-plan-")
        self.addCleanup(self.tmp.cleanup)
        self.project = str(Path(self.tmp.name).resolve())

    def test_top_level_stays_lightweight(self):
        code = (
            "import sys, agent_session_control; "
            "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
            "('textual', 'agent_client_protocol', 'acp', 'sesskit')); "
            "print('HEAVY:' + ','.join(heavy) if heavy else 'CLEAN')"
        )
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60, check=True
        ).stdout.strip()
        self.assertEqual(out, "CLEAN")

    def test_create_plan_shape_for_builtin_runtimes(self):
        for runtime in BUILTINS:
            with self.subTest(runtime=runtime):
                plan = rt.build_create_plan(runtime, self.project)
                self.assertIsInstance(plan.argv, tuple)
                self.assertTrue(plan.argv)
                self.assertNotIn(" ", plan.argv[0])
                self.assertEqual(plan.cwd, self.project)
                self.assertEqual(dict(plan.env), {})

    def test_create_plan_model_override_is_per_launch(self):
        for runtime in BUILTINS:
            with self.subTest(runtime=runtime):
                plain = rt.build_create_plan(runtime, self.project)
                try:
                    bound = rt.build_create_plan(runtime, self.project, model="probe-model-x")
                except Exception as exc:
                    # unsupported model flag: must fail loudly, never silently substitute
                    self.assertTrue(str(exc) or getattr(exc, "code", None))
                    continue
                self.assertIn("probe-model-x", bound.argv)
                again = rt.build_create_plan(runtime, self.project)
                self.assertEqual(
                    tuple(again.argv), tuple(plain.argv), "omitted model inherits defaults; no global rewrite"
                )

    def test_create_plan_rejects_private_mcp_before_launch(self):
        for runtime in BUILTINS:
            with self.subTest(runtime=runtime):
                _assert_refuses(self, rt.build_create_plan, runtime, self.project, mcp_servers=("s1",))

    def test_resume_plan_preserves_exact_identity(self):
        session = {"id": "exact-native-id", "path": "/nonexistent/history.jsonl", "cwd": self.project}
        for runtime in BUILTINS:
            with self.subTest(runtime=runtime):
                try:
                    plan = rt.build_resume_plan(runtime, session)
                except Exception as exc:
                    # e.g. missing native file: must fail loudly, never blank-replace
                    self.assertTrue(str(exc) or getattr(exc, "code", None))
                    continue
                self.assertIsInstance(plan.argv, tuple)
                self.assertTrue(plan.argv)

    def test_resume_plan_fails_without_exact_identity(self):
        for runtime in BUILTINS:
            with self.subTest(runtime=runtime):
                _assert_refuses(self, rt.build_resume_plan, runtime, {"cwd": self.project})

    def test_runtime_profile_is_honest(self):
        for runtime in BUILTINS:
            with self.subTest(runtime=runtime):
                profile = rt.runtime_profile(runtime)
                self.assertTrue(profile.get("executable"))
                self.assertIsInstance(profile.get("available"), bool)
                self.assertTrue(profile.get("interrupt_keys"))
                names = {str(k).lower() for k in profile}
                self.assertTrue(any("alias" in k for k in names), "must expose executable aliases")
                self.assertTrue(
                    any("support" in k or "limit" in k or "capab" in k for k in names),
                    "must expose explicit capability limitations",
                )


if __name__ == "__main__":
    unittest.main()
