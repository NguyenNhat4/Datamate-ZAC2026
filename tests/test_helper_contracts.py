"""Checks for the worker helper reference, using only synthetic fixtures."""
import ast
import importlib.util
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from smolagents.local_python_executor import CodeOutput

from native_support import ScriptedModel, response
from tabular_agent.agent import NativeAgent
from tabular_agent.contracts import validate_answer
from tabular_agent.helper_contracts import HELPER_CONTRACTS, helper_reference


ROOT = Path(__file__).resolve().parents[1]


def load_worker_modules():
    """Load the same trusted source files as the sandbox without starting a worker."""
    modules = {}
    for name in ("data_tools", "tools"):
        spec = importlib.util.spec_from_file_location(name, ROOT / "tabular_agent" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", modules):
            spec.loader.exec_module(module)
        modules[name] = module
    return modules


def documented_signature(text):
    namespace = {}
    exec(f"def documented{text}: pass", namespace)
    return inspect.signature(namespace["documented"])


class CapturingModel(ScriptedModel):
    def generate(self, messages, **kwargs):
        self.visible_text = "\n".join(part["text"] for message in messages for part in message.content
                                      if part.get("type") == "text")
        return super().generate(messages, **kwargs)


class RecordingExecutor:
    def __init__(self, *args, **kwargs):
        self.dispatches = 0

    def manifest(self):
        return {"files": []}

    def send_variables(self, variables):
        pass

    def send_tools(self, tools):
        pass

    def __call__(self, code):
        return CodeOutput({"type": "abstain", "reason": "fixture"}, "", True)

    def validate_final(self, answer):
        return validate_answer(answer)

    def cleanup(self):
        pass


class HelperContracts(unittest.TestCase):
    def test_documented_helpers_match_worker_registry_and_signatures(self):
        tree = ast.parse((ROOT / "tabular_agent/worker.py").read_text())
        initialize = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "initialize")
        scope = next(node.value for node in initialize.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "scope" for target in node.targets))
        worker_names = {key.value for key in scope.keys if isinstance(key, ast.Constant)}
        modules = load_worker_modules()
        data = modules["data_tools"]
        tools = modules["tools"].DataTools
        contracts = {name: signature for name, signature, _ in HELPER_CONTRACTS}
        self.assertEqual(len(contracts), len(HELPER_CONTRACTS))
        bindings = {key.value: ast.unparse(value) for key, value in zip(scope.keys, scope.values)
                    if isinstance(key, ast.Constant)}
        self.assertEqual(set(contracts), worker_names - {
            "__name__", "DATA_DIR", "CALENDAR_PATH", "AS_OF_DATE", "FILES", "final_answer"})
        for name, signature in contracts.items():
            expected_binding = f"data_tools.{name}" if name in {"parse_dates", "parse_numbers", "checked_join", "split_units"} else f"tools.{name}"
            self.assertEqual(bindings[name], expected_binding)
            if name == "read_table":
                # DataTools forwards read options to this implementation.
                actual = inspect.signature(data.read_table)
                params = list(actual.parameters.values())
                params[0] = params[0].replace(name="name")
                params[1:] = [parameter.replace(kind=inspect.Parameter.KEYWORD_ONLY) for parameter in params[1:]]
                actual = actual.replace(parameters=params)
                wrapper = inspect.signature(tools.read_table)
                wrapper.bind(None, "F1", **{parameter.name: parameter.default for parameter in params[1:]})
                with self.assertRaises(TypeError):
                    wrapper.bind(None, "F1", "January")
            elif hasattr(tools, name):
                actual = inspect.signature(getattr(tools, name))
                actual = actual.replace(parameters=list(actual.parameters.values())[1:])
            else:
                actual = inspect.signature(getattr(data, name))
            with self.subTest(name=name):
                self.assertEqual(documented_signature(signature), actual)

    def test_read_wrappers_forward_options_to_reader(self):
        tree = ast.parse((ROOT / "tabular_agent/tools.py").read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DataTools")
        methods = {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}
        for name, target in (("read_table", "data_tools.read_table"),
                             ("read_workbook", "self.read_table"),
                             ("table_profile", "self.read_table")):
            calls = [node for node in ast.walk(methods[name]) if isinstance(node, ast.Call)
                     and ast.unparse(node.func) == target]
            with self.subTest(name=name):
                self.assertTrue(any(any(kw.arg is None and isinstance(kw.value, ast.Name)
                                        and kw.value.id == "options" for kw in call.keywords)
                                    for call in calls))

    def test_rendered_reference_reaches_the_model(self):
        model = CapturingModel([response("final_answer({'type':'abstain','reason':'fixture'})")])
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            calendar = root / "calendar.csv"
            calendar.write_text("date\n")
            answer, _ = NativeAgent(model, executor_factory=RecordingExecutor).solve(
                {"question": "Example question", "as_of_date": "2026-03-04"},
                root, calendar, "Current rulebook")
        self.assertEqual(answer["type"], "abstain")
        self.assertEqual(model.calls, 1)
        visible = model.visible_text
        for name, signature, _ in HELPER_CONTRACTS:
            self.assertIn(name + signature, visible)
        self.assertIn("Current rulebook", visible)
        self.assertIn("Example question", visible)
        self.assertIn("Reload the sources and recompute", visible)
        for constant in ("DATA_DIR", "FILES", "CALENDAR_PATH", "AS_OF_DATE"):
            self.assertIn(constant, visible)
        for rule in ("current rulebook", "all relevant monthly", "unanswerable", "abstain", "required_columns"):
            self.assertIn(rule, visible)
        self.assertIn("without sheet, return sheet names", helper_reference())
        self.assertIn("without table, return table names", helper_reference())


if __name__ == "__main__":
    unittest.main()
