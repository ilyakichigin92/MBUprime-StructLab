"""Keep engine and desktop checks selectable without hiding missing dependencies."""

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        filename = item.path.name
        if filename == "test_detail_keyboard_access_contract.py":
            item.add_marker(pytest.mark.gui)
        elif filename in {
            "test_engine_edges.py",
            "test_external_engines.py",
            "test_gui_export_safety_contract.py",
            "test_rnastructure_thermo_contract.py",
            "test_native_synthetic_runtime.py",
        }:
            item.add_marker(pytest.mark.engine)
