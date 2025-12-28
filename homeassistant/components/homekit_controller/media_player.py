"""Support for HomeKit Controller Televisions."""

from __future__ import annotations

import logging
from typing import Any

from aiohomekit.model.characteristics import (
    Characteristic,
    CharacteristicPermissions,
    CharacteristicsTypes,
    CurrentMediaStateValues,
    RemoteKeyValues,
    TargetMediaStateValues,
)
from aiohomekit.model.services import Service, ServicesTypes
from aiohomekit.utils import clamp_enum_to_char

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KNOWN_DEVICES
from .connection import HKDevice
from .entity import HomeKitEntity

_LOGGER = logging.getLogger(__name__)


REMOTE_KEY_MEDIA_TYPE = "remote_key"

HK_TO_HA_STATE = {
    CurrentMediaStateValues.PLAYING: MediaPlayerState.PLAYING,
    CurrentMediaStateValues.PAUSED: MediaPlayerState.PAUSED,
    CurrentMediaStateValues.STOPPED: MediaPlayerState.IDLE,
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Homekit television."""
    hkid: str = config_entry.data["AccessoryPairingID"]
    conn: HKDevice = hass.data[KNOWN_DEVICES][hkid]

    @callback
    def async_add_service(service: Service) -> bool:
        if service.type != ServicesTypes.TELEVISION:
            return False
        info = {"aid": service.accessory.aid, "iid": service.iid}
        entity = HomeKitTelevision(conn, info)
        conn.async_migrate_unique_id(
            entity.old_unique_id, entity.unique_id, Platform.MEDIA_PLAYER
        )
        async_add_entities([entity])
        return True

    conn.add_listener(async_add_service)


class HomeKitTelevision(HomeKitEntity, MediaPlayerEntity):
    """Representation of a HomeKit Controller Television."""

    _attr_device_class = MediaPlayerDeviceClass.TV

    def get_characteristic_types(self) -> list[str]:
        """Define the homekit characteristics the entity cares about."""
        return [
            CharacteristicsTypes.ACTIVE,
            CharacteristicsTypes.CURRENT_MEDIA_STATE,
            CharacteristicsTypes.TARGET_MEDIA_STATE,
            CharacteristicsTypes.REMOTE_KEY,
            CharacteristicsTypes.ACTIVE_IDENTIFIER,
            CharacteristicsTypes.MUTE,
            CharacteristicsTypes.VOLUME,
            # Characterics that are on the linked INPUT_SOURCE services
            CharacteristicsTypes.CONFIGURED_NAME,
            CharacteristicsTypes.IDENTIFIER,
        ]

    def _speaker_service(self) -> Service | None:
        """Return the linked speaker service for the tv."""
        this_accessory = self._accessory.entity_map.aid(self._aid)
        this_tv = this_accessory.services.iid(self._iid)
        return this_accessory.services.first(
            service_type=ServicesTypes.SPEAKER, parent_service=this_tv
        )

    def _speaker_char(self, char_type: str) -> Characteristic | None:
        """Return a speaker characteristic if available."""
        if not (speaker := self._speaker_service()):
            return None

        if not speaker.has(char_type):
            return None

        return speaker[char_type]

    async def _async_put_speaker_characteristics(
        self, characteristics: dict[str, int | bool]
    ) -> None:
        """Write speaker characteristics to the device."""
        if not (speaker := self._speaker_service()):
            return

        payload = speaker.build_update(characteristics)
        await self._accessory.put_characteristics(payload)

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Flag media player features that are supported."""
        features = MediaPlayerEntityFeature.TURN_OFF | MediaPlayerEntityFeature.TURN_ON

        if self.service.has(CharacteristicsTypes.ACTIVE_IDENTIFIER):
            features |= MediaPlayerEntityFeature.SELECT_SOURCE

        if self.service.has(CharacteristicsTypes.TARGET_MEDIA_STATE):
            if TargetMediaStateValues.PAUSE in self.supported_media_states:
                features |= MediaPlayerEntityFeature.PAUSE

            if TargetMediaStateValues.PLAY in self.supported_media_states:
                features |= MediaPlayerEntityFeature.PLAY

            if TargetMediaStateValues.STOP in self.supported_media_states:
                features |= MediaPlayerEntityFeature.STOP

        if self.service.has(CharacteristicsTypes.REMOTE_KEY):
            features |= MediaPlayerEntityFeature.PLAY_MEDIA

            if RemoteKeyValues.PLAY_PAUSE in self.supported_remote_keys:
                features |= MediaPlayerEntityFeature.PAUSE | MediaPlayerEntityFeature.PLAY

            if RemoteKeyValues.NEXT_TRACK in self.supported_remote_keys:
                features |= MediaPlayerEntityFeature.NEXT_TRACK

            if RemoteKeyValues.PREVIOUS_TRACK in self.supported_remote_keys:
                features |= MediaPlayerEntityFeature.PREVIOUS_TRACK

        if (
            (volume_char := self._speaker_char(CharacteristicsTypes.VOLUME))
            and CharacteristicPermissions.paired_write in volume_char.perms
        ):
            features |= MediaPlayerEntityFeature.VOLUME_SET

        if (
            (mute_char := self._speaker_char(CharacteristicsTypes.MUTE))
            and CharacteristicPermissions.paired_write in mute_char.perms
        ):
            features |= MediaPlayerEntityFeature.VOLUME_MUTE

        return features

    @property
    def supported_media_states(self) -> set[TargetMediaStateValues]:
        """Mediate state flags that are supported."""
        if not self.service.has(CharacteristicsTypes.TARGET_MEDIA_STATE):
            return set()

        return clamp_enum_to_char(
            TargetMediaStateValues,
            self.service[CharacteristicsTypes.TARGET_MEDIA_STATE],
        )

    @property
    def supported_remote_keys(self) -> set[int]:
        """Remote key buttons that are supported."""
        if not self.service.has(CharacteristicsTypes.REMOTE_KEY):
            return set()

        char = self.service[CharacteristicsTypes.REMOTE_KEY]
        if valid_values := getattr(char, "valid_values", None):
            return set(valid_values)

        return clamp_enum_to_char(RemoteKeyValues, char)

    def _normalize_remote_key_name(self, media_id: str) -> str:
        """Normalize a remote key name for matching."""
        return media_id.strip().upper().replace("-", "_").replace(" ", "_")

    def _remote_key_from_media_id(self, media_id: str) -> RemoteKeyValues | None:
        """Return the remote key for a media id."""
        normalized = self._normalize_remote_key_name(media_id)
        if normalized in RemoteKeyValues.__members__:
            return RemoteKeyValues[normalized]

        try:
            value = int(media_id)
        except ValueError:
            return None
        try:
            return RemoteKeyValues(value)
        except ValueError:
            return None

    async def _async_send_remote_key(self, remote_key: RemoteKeyValues) -> None:
        """Send a remote key to the TV if supported."""
        if remote_key.value not in self.supported_remote_keys:
            _LOGGER.debug("Remote key %s is not supported", remote_key)
            return

        await self.async_put_characteristics(
            {CharacteristicsTypes.REMOTE_KEY: remote_key.value}
        )

    @property
    def source_list(self) -> list[str]:
        """List of all input sources for this television."""
        sources = []

        this_accessory = self._accessory.entity_map.aid(self._aid)
        this_tv = this_accessory.services.iid(self._iid)

        input_sources = this_accessory.services.filter(
            service_type=ServicesTypes.INPUT_SOURCE,
            parent_service=this_tv,
        )

        for input_source in input_sources:
            char = input_source[CharacteristicsTypes.CONFIGURED_NAME]
            sources.append(char.value)
        return sources

    @property
    def source(self) -> str | None:
        """Name of the current input source."""
        active_identifier = self.service.value(CharacteristicsTypes.ACTIVE_IDENTIFIER)
        if not active_identifier:
            return None

        this_accessory = self._accessory.entity_map.aid(self._aid)
        this_tv = this_accessory.services.iid(self._iid)

        input_source = this_accessory.services.first(
            service_type=ServicesTypes.INPUT_SOURCE,
            characteristics={CharacteristicsTypes.IDENTIFIER: active_identifier},
            parent_service=this_tv,
        )
        assert input_source
        char = input_source[CharacteristicsTypes.CONFIGURED_NAME]
        return char.value

    @property
    def state(self) -> MediaPlayerState:
        """State of the tv."""
        active = self.service.value(CharacteristicsTypes.ACTIVE)
        if not active:
            return MediaPlayerState.OFF

        homekit_state = self.service.value(CharacteristicsTypes.CURRENT_MEDIA_STATE)
        if homekit_state is not None:
            return HK_TO_HA_STATE.get(homekit_state, MediaPlayerState.ON)

        return MediaPlayerState.ON

    @property
    def volume_level(self) -> float | None:
        """Return the current volume level."""
        if not (volume_char := self._speaker_char(CharacteristicsTypes.VOLUME)):
            return None

        if (volume := volume_char.value) is None:
            return None

        max_value = volume_char.maxValue or 100
        if max_value == 0:
            return 0
        return volume / max_value

    @property
    def is_volume_muted(self) -> bool | None:
        """Return the mute status."""
        if not (mute_char := self._speaker_char(CharacteristicsTypes.MUTE)):
            return None

        if (mute := mute_char.value) is None:
            return None

        return bool(mute)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1."""
        if not (volume_char := self._speaker_char(CharacteristicsTypes.VOLUME)):
            return

        min_value = volume_char.minValue or 0
        max_value = volume_char.maxValue or 100
        min_step = volume_char.minStep or 1
        level = round(volume * max_value)
        level = max(min_value, min(max_value, level))
        level = round(level / min_step) * min_step
        await self._async_put_speaker_characteristics(
            {CharacteristicsTypes.VOLUME: level}
        )

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute the media player."""
        if not self._speaker_char(CharacteristicsTypes.MUTE):
            return

        await self._async_put_speaker_characteristics(
            {CharacteristicsTypes.MUTE: mute}
        )

    async def async_turn_on(self) -> None:
        """Turn the tv on."""
        await self.async_put_characteristics({CharacteristicsTypes.ACTIVE: 1})

    async def async_turn_off(self) -> None:
        """Turn the tv off."""
        await self.async_put_characteristics({CharacteristicsTypes.ACTIVE: 0})

    async def async_media_play(self) -> None:
        """Send play command."""
        if self.state == MediaPlayerState.PLAYING:
            _LOGGER.debug("Cannot play while already playing")
            return

        if TargetMediaStateValues.PLAY in self.supported_media_states:
            await self.async_put_characteristics(
                {CharacteristicsTypes.TARGET_MEDIA_STATE: TargetMediaStateValues.PLAY}
            )
        elif RemoteKeyValues.PLAY_PAUSE in self.supported_remote_keys:
            await self.async_put_characteristics(
                {CharacteristicsTypes.REMOTE_KEY: RemoteKeyValues.PLAY_PAUSE}
            )

    async def async_media_pause(self) -> None:
        """Send pause command."""
        if self.state == MediaPlayerState.PAUSED:
            _LOGGER.debug("Cannot pause while already paused")
            return

        if TargetMediaStateValues.PAUSE in self.supported_media_states:
            await self.async_put_characteristics(
                {CharacteristicsTypes.TARGET_MEDIA_STATE: TargetMediaStateValues.PAUSE}
            )
        elif RemoteKeyValues.PLAY_PAUSE in self.supported_remote_keys:
            await self.async_put_characteristics(
                {CharacteristicsTypes.REMOTE_KEY: RemoteKeyValues.PLAY_PAUSE}
            )

    async def async_media_stop(self) -> None:
        """Send stop command."""
        if self.state == MediaPlayerState.IDLE:
            _LOGGER.debug("Cannot stop when already idle")
            return

        if TargetMediaStateValues.STOP in self.supported_media_states:
            await self.async_put_characteristics(
                {CharacteristicsTypes.TARGET_MEDIA_STATE: TargetMediaStateValues.STOP}
            )

    async def async_media_next_track(self) -> None:
        """Send next track command."""
        await self._async_send_remote_key(RemoteKeyValues.NEXT_TRACK)

    async def async_media_previous_track(self) -> None:
        """Send previous track command."""
        await self._async_send_remote_key(RemoteKeyValues.PREVIOUS_TRACK)

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play a piece of media."""
        media_type_value = (
            media_type.value if isinstance(media_type, MediaType) else str(media_type)
        )
        if media_type_value != REMOTE_KEY_MEDIA_TYPE:
            raise ServiceValidationError(
                f"Unsupported media type {media_type}. Supported type: {REMOTE_KEY_MEDIA_TYPE}"
            )

        if (remote_key := self._remote_key_from_media_id(media_id)) is None:
            raise ServiceValidationError(
                f"Unsupported remote key {media_id}. Supported keys: "
                f"{', '.join(key.lower() for key in RemoteKeyValues.__members__)}"
            )

        await self._async_send_remote_key(remote_key)

    async def async_select_source(self, source: str) -> None:
        """Switch to a different media source."""
        this_accessory = self._accessory.entity_map.aid(self._aid)
        this_tv = this_accessory.services.iid(self._iid)

        input_source = this_accessory.services.first(
            service_type=ServicesTypes.INPUT_SOURCE,
            characteristics={CharacteristicsTypes.CONFIGURED_NAME: source},
            parent_service=this_tv,
        )

        if not input_source:
            raise ValueError(f"Could not find source {source}")

        identifier = input_source[CharacteristicsTypes.IDENTIFIER]

        await self.async_put_characteristics(
            {CharacteristicsTypes.ACTIVE_IDENTIFIER: identifier.value}
        )
