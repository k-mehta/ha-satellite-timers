"""Options-flow + satellite-resolution tests (RB-R2-4 / RB-R2-7).

Run with the HA Core 2026.10.4 venv python (voluptuous 0.16.0 — the
architecture's RB-R2-7 validation names this venv explicitly):

    ~/workspace/ha-repro/venv-2026.10.4/bin/python test_options_flow.py

System python3 has no voluptuous, so this file cannot run under it.
Home Assistant itself is STUBBED (no live HA): only real voluptuous is
used, for the schemas. The real config_flow.py, bridge.py, const.py and
__init__.py are loaded from the component directory.

Covers:
- the options flow's fields: Q11 (settled never-ring), Q12 (settled
  never re-send), O1 (unsettled option), primary satellite — and NO Q13
  field and NO sound field anywhere in the flow;
- submitting the settled picks writes them to the entry data dict, and
  the bridge constructor receives them (via resolve_bridge_options,
  the same resolver async_setup_entry uses);
- an unrelated-option submit while O1 is unsettled preserves the
  unsettled option (the three-option select shape);
- satellite resolution (§5.4): caller → primary → single → loud error;
  no "kitchen" substring guess, no sorted-device fallback — a device
  renamed to contain "kitchen" has no effect on routing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPONENTS = HERE / "custom_components" / "satellite_timers"


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        raise SystemExit(1)


# ---------------------------------------------------------------- stubs
def _module(name: str, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    parent, _, _ = name.rpartition(".")
    if parent and parent in sys.modules:
        setattr(sys.modules[parent], name.rpartition(".")[2], mod)
    return mod


class _HomeAssistantError(Exception):
    pass


class _ConfigEntryNotReady(Exception):
    pass


class _SupportsResponse:
    OPTIONAL = "optional"
    ONLY = "only"


class _Platform:
    SENSOR = "sensor"


def _multi_select(options):
    return options


def _cv_string(value):
    return value


def _cv_boolean(value):
    return value


class _FakeConfigFlow:
    VERSION = 1

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()


class _FakeOptionsFlow:
    """Stand-in for HA's OptionsFlow: capture the flow result."""

    def __init__(self):
        self.hass = None
        self.config_entry = None

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(self, *, step_id, data_schema, errors=None):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
        }


class _FakeConfigFlowResult(dict):
    pass


_module("homeassistant")
_module("homeassistant.components")
_module("homeassistant.components.http", StaticPathConfig=object)
_module(
    "homeassistant.components.intent.timers",
    TimerNotFoundError=type("TimerNotFoundError", (Exception,), {}),
    TimersNotSupportedError=type("TimersNotSupportedError", (Exception,), {}),
)
_module("homeassistant.components.lovelace")
_module("homeassistant.components.lovelace.const", LOVELACE_DATA="lovelace")
_module(
    "homeassistant.components.lovelace.resources",
    ResourceStorageCollection=object,
)
_module(
    "homeassistant.config_entries",
    ConfigEntry=object,
    ConfigFlow=_FakeConfigFlow,
    ConfigFlowResult=_FakeConfigFlowResult,
    OptionsFlow=_FakeOptionsFlow,
)
_module("homeassistant.const", Platform=_Platform)
_module(
    "homeassistant.core",
    HomeAssistant=object,
    SupportsResponse=_SupportsResponse,
    callback=lambda func: func,
)
_module(
    "homeassistant.exceptions",
    ConfigEntryNotReady=_ConfigEntryNotReady,
    HomeAssistantError=_HomeAssistantError,
)
_module("homeassistant.helpers")
_module(
    "homeassistant.helpers.config_validation",
    multi_select=_multi_select,
    string=_cv_string,
    boolean=_cv_boolean,
)
_module("homeassistant.helpers.device_registry")
_module("homeassistant.helpers.entity_registry")
class _FakeIntentHandler:
    """Stand-in for HA's intent.IntentHandler (class attrs only)."""

    def async_validate_slots(self, slots):
        return slots


_module(
    "homeassistant.helpers.intent",
    IntentHandler=_FakeIntentHandler,
    IntentHandleError=type("IntentHandleError", (Exception,), {}),
    Intent=object,
    IntentResponse=object,
    async_register=lambda *a, **k: None,
    async_remove=lambda *a, **k: None,
)
_module("homeassistant.helpers.issue_registry")
_module("homeassistant.helpers.collection", ItemNotFound=type("ItemNotFound", (Exception,), {}))

# Real package files, loaded without executing the package __init__.
pkg = types.ModuleType("satellite_timers")
pkg.__path__ = [str(COMPONENTS)]
sys.modules["satellite_timers"] = pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"satellite_timers.{name}", COMPONENTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"satellite_timers.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


const_mod = _load("const")
bridge_mod = _load("bridge")
intents_mod = _load("intents")
flow_mod = _load("config_flow")
init_mod = _load("__init__")

import voluptuous as vol


# ---------------------------------------------------------------- fakes
class FakeDevice:
    def __init__(self, device_id, name):
        self.device_id = device_id
        self.name = name


class FakeEntity:
    def __init__(self, entity_id, device_id):
        self.entity_id = entity_id
        self.device_id = device_id


class FakeTimerManager:
    """Enough of HA's TimerManager for timer_devices/assist_supported."""

    def __init__(self, device_ids):
        self.handlers = {device_id: (lambda *a: None) for device_id in device_ids}
        self.timers = {}

    def register_handler(self, device_id, handler):
        self.handlers[device_id] = handler


class FakeHass:
    def __init__(self, device_ids, devices_by_id):
        self.data = {"intent.timer": FakeTimerManager(device_ids)}
        self._devices = devices_by_id

    # device_registry.async_get(hass) / entity_registry.async_get(hass)
    # are patched per test below via the stub modules.


class FakeConfigEntry:
    def __init__(self, options, data):
        self.options = options
        self.data = data


DEVICES = {
    "dev-kitchen": FakeDevice("dev-kitchen", "kitchen_hub"),
    "dev-iphone": FakeDevice("dev-iphone", "Kshitij's iPhone"),
}


def _install_registries(hass_devices, entities):
    sys.modules[
        "homeassistant.helpers.device_registry"
    ].async_get = lambda hass: _Registry(hass_devices)
    sys.modules[
        "homeassistant.helpers.entity_registry"
    ].async_get = lambda hass: _Registry(entities)


class _Registry:
    def __init__(self, items):
        self._items = items

    def async_get(self, key):
        return self._items.get(key)


def make_flow(device_ids, options=None, data=None):
    """An options flow bound to a fake hass + entry with the given devices."""
    hass = FakeHass(device_ids, DEVICES)
    _install_registries(DEVICES, {})
    flow = flow_mod.SatelliteTimersOptionsFlow()
    flow.hass = hass
    flow.config_entry = FakeConfigEntry(
        options or {}, data or {"devices": device_ids}
    )
    return flow


def schema_fields(schema):
    """{field key: (validator, default)} for a vol.Schema.

    Voluptuous 0.16.0 markers keep the key in ``.schema`` and the
    default behind a ``.default()`` factory callable.
    """
    out = {}
    for key, validator in schema.schema.items():
        default = key.default() if callable(getattr(key, "default", None)) else None
        out[key.schema] = (validator, default)
    return out


# ------------------------------------------------------------- the cases
# 1. The form shows exactly the R2 fields: devices, primary, Q11, Q12,
#    O1 — no Q13, no sound field.
flow = make_flow(["dev-kitchen"])
form = asyncio.run(flow.async_step_init(None))
check("options flow: first call shows the form", form["type"] == "form")
fields = schema_fields(form["data_schema"])
check("options flow: fields are devices/primary/Q11/Q12/O1",
      set(fields) == {
          "devices",
          "primary_satellite",
          "q11_ring_timing",
          "q12_auto_stop_resend",
          "o1_alarm_behaviour",
      })
check("options flow: no Q13 field in the schema",
      not any("q13" in key for key in fields))
check("options flow: no sound field in the schema",
      not any("volume" in key or "quiet" in key or "escalation" in key
              for key in fields))
check("options flow: O1 defaults to the unsettled option",
      fields["o1_alarm_behaviour"][1] == "unsettled")
check("options flow: Q11 defaults to settled never-ring",
      fields["q11_ring_timing"][1] == "never_ring")
check("options flow: Q12 defaults to settled never re-send",
      fields["q12_auto_stop_resend"][1] == "never_resend")
check("options flow: primary defaults to unset",
      fields["primary_satellite"][1] == "")
# The selects accept the settled literals (vol.In containers).
q11_validator, _ = fields["q11_ring_timing"]
check("options flow: Q11 select accepts never_ring",
      "never_ring" in q11_validator.container)
o1_validator, _ = fields["o1_alarm_behaviour"]
check("options flow: O1 select accepts all three options",
      set(o1_validator.container)
      == {"unsettled", "rings_until_cancelled", "auto_stop"})
flow_text = (COMPONENTS / "config_flow.py").read_text()
check("options flow: config_flow.py has no Q13 key or constant",
      "q13_" not in flow_text.lower() and "CONF_Q13" not in flow_text)
check("options flow: config_flow.py has no sound reference",
      "sound" not in flow_text.lower())

# 2. Submitting the settled picks writes them to the entry data dict,
#    and the bridge constructor receives them.
settled_input = {
    "devices": ["dev-kitchen"],
    "primary_satellite": "",
    "q11_ring_timing": "never_ring",
    "q12_auto_stop_resend": "never_resend",
    "o1_alarm_behaviour": "unsettled",
}
result = asyncio.run(flow.async_step_init(settled_input))
check("options flow: settled submit creates an entry", result["type"] == "create_entry")
check("options flow: settled submit writes the settled picks",
      result["data"] == {
          "devices": ["dev-kitchen"],
          "primary_satellite": "",
          "q11_ring_timing": "never_ring",
          "q12_auto_stop_resend": "never_resend",
          "o1_alarm_behaviour": "unsettled",
      })
resolved = bridge_mod.resolve_bridge_options(result["data"])
bridge = bridge_mod.SatelliteTimerBridge(FakeHass(["dev-kitchen"], DEVICES),
                                         ["dev-kitchen"], resolved)
check("options flow: bridge constructor receives the settled picks",
      bridge._queue_options["q11_ring_timing"] == "never_ring"
      and bridge._queue_options["q12_auto_stop_resend"] == "never_resend")
check("options flow: bridge O1 is unsettled from the settled submit",
      bridge._o1_alarm_behaviour is None)
check("options flow: bridge primary is unset from the settled submit",
      bridge.primary_satellite is None)

# 3. Submitting O1 settled "rings until cancelled" reaches the bridge.
flow2 = make_flow(["dev-kitchen"])
result2 = asyncio.run(flow2.async_step_init({
    **settled_input, "o1_alarm_behaviour": "rings_until_cancelled",
}))
bridge2 = bridge_mod.SatelliteTimerBridge(
    FakeHass(["dev-kitchen"], DEVICES), ["dev-kitchen"],
    bridge_mod.resolve_bridge_options(result2["data"]))
check("options flow: settled O1 reaches the bridge constructor",
      bridge2._o1_alarm_behaviour == "rings_until_cancelled")

# 4. Preservation: an unrelated-option submit while O1 is unsettled
#    preserves the unsettled option (the form pre-fills with it, the
#    submit writes it back unchanged).
flow3 = make_flow(["dev-kitchen"], options={
    "devices": ["dev-kitchen"],
    "primary_satellite": "",
    "q11_ring_timing": "never_ring",
    "q12_auto_stop_resend": "never_resend",
    "o1_alarm_behaviour": "unsettled",
})
check("options flow: _current_values pre-fills O1 unsettled",
      flow3._current_values()["o1_alarm_behaviour"] == "unsettled")
form3 = asyncio.run(flow3.async_step_init(None))
check("options flow: form pre-fills O1 unsettled",
      schema_fields(form3["data_schema"])["o1_alarm_behaviour"][1] == "unsettled")
result3 = asyncio.run(flow3.async_step_init(settled_input))
check("options flow: unrelated submit preserves O1 unsettled",
      result3["data"]["o1_alarm_behaviour"] == "unsettled")

# 4a. Critic F1: Q11/Q12 are settled answers, not choices — the form
#     offers ONLY the settled literal for each, a submit carrying a
#     rejected literal is clamped to the settled value, and a stale
#     stored rejected value pre-fills as settled.
flowF1 = make_flow(["dev-kitchen"])
formF1 = asyncio.run(flowF1.async_step_init(None))
fieldsF1 = schema_fields(formF1["data_schema"])
q11_validator_F1, _ = fieldsF1["q11_ring_timing"]
check("options flow F1: Q11 select offers only the settled literal",
      set(q11_validator_F1.container) == {"never_ring"})
q12_validator_F1, _ = fieldsF1["q12_auto_stop_resend"]
check("options flow F1: Q12 select offers only the settled literal",
      set(q12_validator_F1.container) == {"never_resend"})
resultF1 = asyncio.run(flowF1.async_step_init({
    **settled_input,
    "q11_ring_timing": "ring_on_liveness_drain",
    "q12_auto_stop_resend": "resend_once_per_registration",
}))
check("options flow F1: rejected Q11/Q12 submit clamps to settled",
      resultF1["data"]["q11_ring_timing"] == "never_ring"
      and resultF1["data"]["q12_auto_stop_resend"] == "never_resend")
check("options flow F1: rejected submit still records O1 + devices",
      resultF1["data"]["devices"] == ["dev-kitchen"]
      and resultF1["data"]["o1_alarm_behaviour"] == "unsettled")
flowF1b = make_flow(["dev-kitchen"], options={
    "devices": ["dev-kitchen"],
    "primary_satellite": "",
    "q11_ring_timing": "ring_at_bridge_start",
    "q12_auto_stop_resend": "resend_once_per_registration",
    "o1_alarm_behaviour": "unsettled",
})
check("options flow F1: stale rejected values pre-fill as settled",
      flowF1b._current_values()["q11_ring_timing"] == "never_ring"
      and flowF1b._current_values()["q12_auto_stop_resend"] == "never_resend")
# The O1 three-option shape is unaffected by the clamp.
check("options flow F1: O1 still offers all three options",
      set(fieldsF1["o1_alarm_behaviour"][0].container)
      == {"unsettled", "rings_until_cancelled", "auto_stop"})

# 4b. Critic F2: Configure is submittable while a satellite is
#     disconnected. No live handler for dev-kitchen, but the entry has
#     it configured: the form must offer it, the schema must validate
#     a submit naming it (the real HA validation — the 400 was
#     "<id> is not a valid option"), and the submit must land it.
flowF2 = make_flow([], data={"devices": ["dev-kitchen"]})
formF2 = asyncio.run(flowF2.async_step_init(None))
fieldsF2 = schema_fields(formF2["data_schema"])
check("options flow F2: form pre-fills the disconnected device",
      fieldsF2["devices"][1] == ["dev-kitchen"])
# The test stubs cv.multi_select to the options dict itself (see
# _multi_select above), so "offered" here is membership in that dict;
# the REAL voluptuous validation — the "<id> is not a valid option"
# 400 — is proven on the rig (fix1 leg B), where real HA validates
# the submit against this same dict.
check("options flow F2: disconnected device is offered in the form",
      "dev-kitchen" in fieldsF2["devices"][0])
check("options flow F2: unconfigured device is not offered",
      "dev-ghost" not in fieldsF2["devices"][0])
resultF2 = asyncio.run(flowF2.async_step_init({
    **settled_input, "devices": ["dev-kitchen"],
}))
check("options flow F2: submit with the disconnected device lands",
      resultF2["type"] == "create_entry"
      and resultF2["data"]["devices"] == ["dev-kitchen"])

# 5. Satellite resolution (§5.4): caller → primary → single → loud error.
resolve = init_mod._resolve_satellite_device


def make_bridge(device_ids, options=None):
    return bridge_mod.SatelliteTimerBridge(
        FakeHass(device_ids, DEVICES), device_ids,
        bridge_mod.resolve_bridge_options(options))


def expect_error(label, fn, *needles):
    try:
        fn()
    except _HomeAssistantError as err:
        check(label, all(n in str(err) for n in needles))
        return
    check(label, False)


hass = FakeHass(["dev-kitchen", "dev-iphone"], DEVICES)
_install_registries(DEVICES, {
    "assist_satellite.kitchen": FakeEntity("assist_satellite.kitchen", "dev-kitchen"),
    "assist_satellite.iphone": FakeEntity("assist_satellite.iphone", "dev-iphone"),
    "assist_satellite.ghost": FakeEntity("assist_satellite.ghost", "dev-ghost"),
})
two = make_bridge(["dev-kitchen", "dev-iphone"])
check("resolution: caller satellite wins",
      resolve(hass, two, "assist_satellite.iphone") == "dev-iphone")
expect_error("resolution: unknown caller satellite names the voice alternative",
             lambda: resolve(hass, two, "assist_satellite.nope"),
             "was not found", "Okay Nabu")
expect_error("resolution: caller satellite on an unconfigured device errors",
             lambda: resolve(hass, two, "assist_satellite.ghost"),
             "not a configured timer satellite", "Okay Nabu")
check("resolution: primary wins over the caller-less default",
      resolve(hass, make_bridge(
          ["dev-kitchen", "dev-iphone"],
          {"primary_satellite": "dev-iphone"}), None) == "dev-iphone")
expect_error("resolution: two satellites, no primary, errors loudly with the Configure path",
             lambda: resolve(hass, two, None),
             "Configure", "Okay Nabu")
# The "kitchen" name has no effect: a device renamed to contain
# "kitchen" must not silently win routing (N1 — the substring guess
# is deleted). The two-device house still errors loudly.
expect_error("resolution: device named kitchen does not win routing",
             lambda: resolve(hass, two, None),
             "Configure")
check("resolution: single device resolves without a primary",
      resolve(hass, make_bridge(["dev-kitchen"]), None) == "dev-kitchen")
expect_error("resolution: primary naming an unconfigured device errors with the Configure path",
             lambda: resolve(hass, make_bridge(
                 ["dev-kitchen"],
                 {"primary_satellite": "dev-iphone"}), None),
             "Configure")
expect_error("resolution: no devices names the voice alternative",
             lambda: resolve(hass, make_bridge([]), None),
             "No satellite is configured", "Okay Nabu")

print("\nAll options-flow checks passed.")
