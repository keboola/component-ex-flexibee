"""Functional tests for component using VCR cassettes."""

import json
from pathlib import Path

import pytest
from keboola.datadirtest.vcr import VCRDataDirTester, get_test_cases
from keboola.datadirtest.vcr.tester import VCRTestDataDir

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")


class StateAwareVCRTestDataDir(VCRTestDataDir):
    """VCRTestDataDir that preserves source/data/in/state.json instead of overwriting it.

    The base class ``_override_input_state`` always writes ``{}`` when no
    ``last_state_override`` is provided, which wipes any watermark we put in
    the fixture.  This subclass reads the fixture's own state file and uses it
    as the override so the framework copies it correctly to the temp dir.
    """

    def setUp(self):
        # Load the state from the fixture before the base class overwrites it.
        state_path = Path(self.orig_dir) / "source" / "data" / "in" / "state.json"
        if state_path.exists() and self._input_state_override is None:
            try:
                with open(state_path) as f:
                    self._input_state_override = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        super().setUp()


@pytest.mark.parametrize("test_name", get_test_cases(FUNCTIONAL_DIR))
def test_functional(test_name):
    """Run a single VCR functional test case."""
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
        test_data_dir_class=StateAwareVCRTestDataDir,
    )
    tester.run()
