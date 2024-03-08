"""Platform to present any Tuya DP as a remote."""

import asyncio
import json
import base64
import logging
from functools import partial
import struct
from enum import StrEnum
from typing import Any, Iterable
from .config_flow import _col_to_select

import voluptuous as vol
from homeassistant.components.remote import (
    ATTR_ACTIVITY,
    ATTR_COMMAND,
    ATTR_COMMAND_TYPE,
    ATTR_NUM_REPEATS,
    ATTR_DELAY_SECS,
    ATTR_DEVICE,
    ATTR_TIMEOUT,
    DOMAIN,
    RemoteEntity,
    RemoteEntityFeature,
)
from homeassistant.components import persistent_notification
from homeassistant.const import CONF_DEVICE_ID, STATE_OFF
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.storage import Store

from .common import LocalTuyaEntity, async_setup_entry
from .const import CONF_RECEIVE_DP

NSDP_CONTROL = "control"  # The control commands
NSDP_TYPE = "type"  # The identifier of an IR library
NSDP_HEAD = "head"  # Actually used but not documented
NSDP_KEY1 = "key1"  # Actually used but not documented

_LOGGER = logging.getLogger(__name__)


class ControlType(StrEnum):
    SEND_IR = "send_ir"
    STUDY = "study"
    STUDY_EXIT = "study_exit"


class RemoteDP(StrEnum):
    DP_SEND = "201"
    DP_RECIEVE = "202"


CODE_STORAGE_VERSION = 1
SOTRAGE_KEY = "localtuya_remotes_codes"


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        vol.Optional(
            CONF_RECEIVE_DP, default=RemoteDP.DP_RECIEVE.value
        ): _col_to_select(dps, is_dps=True),
    }


class LocalTuyaRemote(LocalTuyaEntity, RemoteEntity):
    """Representation of a Tuya remote."""

    def __init__(
        self,
        device,
        config_entry,
        remoteid,
        **kwargs,
    ):
        """Initialize the Tuya remote."""
        super().__init__(device, config_entry, remoteid, _LOGGER, **kwargs)

        self._dp_send = str(self._config.get(self._dp_id, RemoteDP.DP_SEND))
        self._dp_recieve = str(self._config.get(CONF_RECEIVE_DP, RemoteDP.DP_RECIEVE))

        self._device_id = self._device_config[CONF_DEVICE_ID]

        # self._attr_activity_list: list = []
        # self._attr_current_activity: str | None = None

        self._last_code = None

        self._codes = {}

        self._codes_storage = Store(self._hass, CODE_STORAGE_VERSION, SOTRAGE_KEY)

        self._storage_loaded = False

        self._attr_supported_features = (
            RemoteEntityFeature.LEARN_COMMAND | RemoteEntityFeature.DELETE_COMMAND
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the remote."""
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the remote."""
        self._attr_is_on = False
        self.async_write_ha_state()

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        """Send commands to a device."""
        if not self._attr_is_on:
            raise ServiceValidationError(f"Remote {self.entity_id} is turned off")

        commands = command
        device = kwargs.get(ATTR_DEVICE)

        repeats: int = kwargs.get(ATTR_NUM_REPEATS)
        repeats_delay: float = kwargs.get(ATTR_DELAY_SECS)

        for req in [device, commands]:
            if not req:
                raise ServiceValidationError("Missing required fields")

        if not self._storage_loaded:
            await self._async_load_storage()

        # base64_code = ""
        # if base64_code is None:
        #     option_value = ""
        #     _LOGGER.debug("Sending Option: -> " + option_value)

        #     pulses = self.pronto_to_pulses(option_value)
        #     base64_code = "1" + self.pulses_to_base64(pulses)
        for command in commands:
            code = self._get_code(device, command)

            base64_code = "1" + code
            if repeats:
                current_repeat = 0
                while current_repeat < repeats:
                    await self.send_signal(ControlType.SEND_IR, base64_code)
                    if repeats_delay:
                        await asyncio.sleep(repeats_delay)
                    current_repeat += 1
                continue

            await self.send_signal(ControlType.SEND_IR, base64_code)

    async def async_learn_command(self, **kwargs: Any) -> None:
        """Learn a command from a device."""
        if not self._attr_is_on:
            raise ServiceValidationError(f"Remote {self.entity_id} is turned off")

        now, timeout = 0, kwargs.get(ATTR_TIMEOUT, 30)
        sucess = False

        device = kwargs.get(ATTR_DEVICE)
        commands = kwargs.get(ATTR_COMMAND)
        # command_type = kwargs.get(ATTR_COMMAND_TYPE)
        for req in [device, commands]:
            if not req:
                raise ServiceValidationError("Missing required fields")

        if not self._storage_loaded:
            await self._async_load_storage()

        for command in commands:
            last_code = self._last_code
            await self.send_signal(ControlType.STUDY)
            persistent_notification.async_create(
                self.hass,
                f"Press the '{command}' button.",
                title="Learn command",
                notification_id="learn_command",
            )

            try:
                while now < timeout:
                    if last_code != (dp_code := self.dp_value(RemoteDP.DP_RECIEVE)):
                        self._last_code = dp_code
                        sucess = True
                        await self.send_signal(ControlType.STUDY_EXIT)
                        break

                    now += 1
                    await asyncio.sleep(1)

                if not sucess:
                    await self.send_signal(ControlType.STUDY_EXIT)
                    raise ServiceValidationError(f"Failed to learn: {command}")

            finally:
                persistent_notification.async_dismiss(
                    self.hass, notification_id="learn_command"
                )

            # code retrive sucess and it's sotred in self._last_code
            # we will store the codes.
            await self._save_new_command(device, command, self._last_code)

            if command != commands[-1]:
                await asyncio.sleep(1)

    async def async_delete_command(self, **kwargs: Any) -> None:
        """Delete commands from the database."""
        device = kwargs.get(ATTR_DEVICE)
        commands = kwargs.get(ATTR_COMMAND)

        for req in [device, command]:
            if not req:
                raise ServiceValidationError("Missing required fields")

        if not self._storage_loaded:
            await self._async_load_storage()

        for command in commands:
            await self._delete_command(device, command)

    async def send_signal(self, control, base64_code=None):
        command = {NSDP_CONTROL: control}

        if control == ControlType.SEND_IR:
            command[NSDP_TYPE] = 0
            command[NSDP_HEAD] = ""
            command[NSDP_KEY1] = base64_code

        await self._device.set_dp(json.dumps(command), self._dp_send)

    async def _delete_command(self, device, command) -> None:
        """Store new code into stoarge."""
        codes_data = self._codes
        ir_controller = self._device_id

        if ir_controller not in codes_data:
            raise ServiceValidationError(f"IR remote hasn't learned any buttons yet.")

        if device not in codes_data[ir_controller]:
            raise ServiceValidationError(f"Couldn't find the device: {device}.")

        commands = codes_data[ir_controller][device]
        if command not in codes_data[ir_controller][device]:
            raise ServiceValidationError(
                f"Couldn't find the command {command} for in {device} device. the available commands for this device is: {list(commands)}"
            )

        codes_data[ir_controller][device].pop(command)
        await self._codes_storage.async_save(codes_data)

    async def _save_new_command(self, device, command, code) -> None:
        """Store new code into stoarge."""
        device_unqiue_id = self._device_id
        codes = self._codes

        if device_unqiue_id not in codes:
            codes[device_unqiue_id] = {}

        # device_data = {command: {ATTR_COMMAND: code, ATTR_COMMAND_TYPE: command_type}}
        device_data = {command: code}

        if device in codes[device_unqiue_id]:
            codes[device_unqiue_id][device].update(device_data)
        else:
            codes[device_unqiue_id][device] = device_data

        await self._codes_storage.async_save(codes)

    async def _async_load_storage(self):
        """Load code and flag storage from disk."""
        # Exception is intentionally not trapped to
        # provide feedback if something fails.
        self._codes.update(await self._codes_storage.async_load() or {})
        self._storage_loaded = True

    # No need to restore state for a remote
    async def restore_state_when_connected(self):
        """Do nothing for a remote."""
        return

    def _get_code(self, device, command):
        """Get the code of command from database."""
        codes_data = self._codes
        ir_controller = self._device_id

        if ir_controller not in codes_data:
            raise ServiceValidationError(f"IR remote hasn't learned any buttons yet.")

        if device not in codes_data[ir_controller]:
            raise ServiceValidationError(f"Couldn't find the device: {device}.")

        commands = codes_data[ir_controller][device]
        if command not in commands:
            raise ServiceValidationError(
                f"Couldn't find the command {command} for in {device} device. the available commands for this device is: {list(commands)}"
            )

        command = codes_data[ir_controller][device][command]

        return command

    def status_updated(self):
        """Device status was updated."""
        state = self.dp_value(self._dp_id)

    def status_restored(self, stored_state: State) -> None:
        """Device status was restored.."""
        state = stored_state
        self._attr_is_on = state is None or state.state != STATE_OFF


async_setup_entry = partial(async_setup_entry, DOMAIN, LocalTuyaRemote, flow_schema)


def pronto_to_pulses(pronto):
    ret = []
    pronto = [int(x, 16) for x in pronto.split(" ")]
    ptype = pronto[0]
    timebase = pronto[1]
    pair1_len = pronto[2]
    pair2_len = pronto[3]
    if ptype != 0:
        # only raw (learned) codes are handled
        return ret
    if timebase < 90 or timebase > 139:
        # only 38 kHz is supported?
        return ret
    pronto = pronto[4:]
    timebase *= 0.241246
    for i in range(0, pair1_len * 2, 2):
        ret += [round(pronto[i] * timebase), round(pronto[i + 1] * timebase)]
    pronto = pronto[pair1_len * 2 :]
    for i in range(0, pair2_len * 2, 2):
        ret += [round(pronto[i] * timebase), round(pronto[i + 1] * timebase)]
    return ret


def pulses_to_base64(pulses):
    fmt = "<" + str(len(pulses)) + "H"
    return base64.b64encode(struct.pack(fmt, *pulses)).decode("ascii")


def base64_to_pulses(code_base_64):
    if len(code_base_64) % 4 == 1 and code_base_64.startswith("1"):
        # code can be padded with "1"
        code_base_64 = code_base_64[1:]
    raw_bytes = base64.b64decode(code_base_64)
    fmt = "<%dH" % (len(raw_bytes) >> 1)
    return list(struct.unpack(fmt, raw_bytes))
