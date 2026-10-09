
from ksound_hub.module_ui_state import checked_state
from ksound_hub.module_extensions import (
    validate_extensions,
)


def surfaces():
    return validate_extensions({
        "id": "demo",
        "extensions": {
            "schema": 1,
            "desktop": [{
                "id": "home",
                "label": "Home",
                "controls": [
                    {
                        "id": "gain",
                        "label": "Gain",
                        "type": "slider",
                        "action": "demo.gain",
                        "min": 0,
                        "max": 10,
                        "default": 5,
                    },
                    {
                        "id": "muted",
                        "label": "Muted",
                        "type": "toggle",
                        "action": "demo.muted",
                    },
                    {
                        "id": "hit",
                        "label": "Hit",
                        "type": "button",
                        "action": "demo.hit",
                    },
                ],
            }],
        },
    })


def test_state_validation():
    specs = surfaces()

    assert checked_state(
        specs,
        {"home": {"gain": 7, "muted": True}},
    ) == {
        "home": {"gain": 7, "muted": True}
    }

    assert checked_state(
        specs,
        {"home": {"gain": True}},
    ) is None

    assert checked_state(
        specs,
        {"home": {"gain": 11}},
    ) is None

    assert checked_state(
        specs,
        {"home": {"hit": True}},
    ) is None

    assert checked_state(
        specs,
        {"other": {"gain": 7}},
    ) is None


def test_remote_snapshot(tmp_path):

    from types import SimpleNamespace

    from ksound_hub.module_remote_bridge import (
        publish,
        read,
    )

    control = SimpleNamespace(
        control_id="gain",
        kind="slider",
        label="Gain",
        action="demo.gain",
        minimum=0,
        maximum=10,
        initial=5,
        options=(),
    )

    surface = SimpleNamespace(
        module_id="demo",
        surface_id="home",
        label="Home",
        icon="◇",
        controls=(control,),
    )

    path = tmp_path / "modules.json"

    publish(
        path,
        (surface,),
        {"demo": {"home": {"gain": 7}}},
    )

    assert read(path)[0]["state"] == {
        "gain": 7
    }

    publish(path, (surface,))

    assert read(path)[0]["state"] == {}
