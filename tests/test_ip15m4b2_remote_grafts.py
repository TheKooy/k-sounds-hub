
import json
import time

from pathlib import Path
from types import SimpleNamespace

from ksound_hub.module_remote_bridge import (
    publish,
    read,
    control_for,
    valid_value,
)


def test_live_and_stale(tmp_path):
    path = tmp_path / "modules.json"

    control = SimpleNamespace(
        control_id="x",
        kind="slider",
        label="Level",
        action="demo.level",
        minimum=0,
        maximum=10,
        initial=5,
        options=(),
    )

    surface = SimpleNamespace(
        module_id="example_widget",
        surface_id="remote_test",
        label="Example",
        icon="◇",
        controls=(control,),
    )

    publish(path, (surface,))

    assert path.stat().st_mode & 0o777 == 0o600

    found = control_for(
        read(path),
        "example_widget",
        "remote_test",
        "x",
    )

    assert valid_value(found, 10)
    assert not valid_value(found, 11)
    assert not valid_value(found, True)

    data = json.loads(
        path.read_text()
    )

    data["expires"] = time.time() - 1
    path.write_text(
        json.dumps(data)
    )

    assert read(path) == []


def test_other_values():
    assert valid_value(
        {"type": "button"},
        True,
    )

    assert not valid_value(
        {"type": "button"},
        False,
    )

    assert valid_value(
        {"type": "toggle"},
        False,
    )

    assert not valid_value(
        {"type": "toggle"},
        0,
    )

    assert valid_value(
        {
            "type": "select",
            "options": [
                {"value": "a"},
            ],
        },
        "a",
    )

    assert not valid_value(
        {
            "type": "select",
            "options": [
                {"value": "a"},
            ],
        },
        "b",
    )
