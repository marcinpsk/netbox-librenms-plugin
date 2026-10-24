"""Interface test modules collect before Django settings are configured."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("module", ["test_acquisition_interfaces.py", "test_interface_tab_source_snapshot.py"])
def test_interface_tests_collect_without_django_settings(module):
    module_path = Path(__file__).with_name(module)
    environment = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}

    result = subprocess.run(
        [sys.executable, "-c", "import runpy, sys; runpy.run_path(sys.argv[1])", str(module_path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
