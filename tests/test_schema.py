"""Regression checks for customer-facing configuration schemas."""

import json
from pathlib import Path


def test_date_field_picker_allows_manual_values():
    """Users can enter a valid date column absent from the metadata response."""
    schema_path = Path(__file__).parent.parent / "component_config" / "configRowSchema.json"
    schema = json.loads(schema_path.read_text())

    assert schema["properties"]["date_field"]["options"]["tags"] is True
