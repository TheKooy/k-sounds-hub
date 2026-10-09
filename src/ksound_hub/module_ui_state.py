"""Optional module-owned UI state contract.

An action acknowledgement is NOT proof of a control's value.

State format:
{
    "surface_id": {
        "control_id": confirmed_value
    }
}
"""

from __future__ import annotations

from .module_extensions import ExtensionSurface


def checked_state(
    surfaces: tuple[ExtensionSurface, ...],
    candidate,
) -> dict | None:

    if (
        not isinstance(candidate, dict)
        or len(candidate) > 36
    ):
        return None

    by_id = {
        surface.surface_id: surface
        for surface in surfaces
    }

    result = {}

    for surface_id, controls in candidate.items():

        if (
            surface_id not in by_id
            or not isinstance(controls, dict)
            or len(controls) > 48
        ):
            return None

        specs = {
            control.control_id: control
            for control in by_id[surface_id].controls
        }

        clean = {}

        for control_id, value in controls.items():

            control = specs.get(control_id)

            if (
                control is None
                or control.kind in ("label", "button")
            ):
                return None

            if control.kind == "slider":
                valid = (
                    type(value) is int
                    and control.minimum
                    <= value
                    <= control.maximum
                )

            elif control.kind == "toggle":
                valid = type(value) is bool

            elif control.kind == "select":
                valid = (
                    type(value) is str
                    and value in {
                        option_value
                        for _, option_value
                        in control.options
                    }
                )

            else:
                return None

            if not valid:
                return None

            clean[control_id] = value

        result[surface_id] = clean

    return result
