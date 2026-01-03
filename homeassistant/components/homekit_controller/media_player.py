"""Support for HomeKit Controller Televisions."""

from __future__ import annotations

import logging

from aiohomekit.model.characteristics import (
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
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KNOWN_DEVICES
from .connection import HKDevice
from .entity import HomeKitEntity

_LOGGER = logging.getLogger(__name__)


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
            # Characterics that are on the linked INPUT_SOURCE services
            CharacteristicsTypes.CONFIGURED_NAME,
            CharacteristicsTypes.IDENTIFIER,
            # Volume control characteristics (may be on linked SPEAKER service)
            CharacteristicsTypes.VOLUME,
            CharacteristicsTypes.VOLUME_SELECTOR,
            CharacteristicsTypes.MUTE,
        ]

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

        if (
            self.service.has(CharacteristicsTypes.REMOTE_KEY)
            and RemoteKeyValues.PLAY_PAUSE in self.supported_remote_keys
        ):
            features |= MediaPlayerEntityFeature.PAUSE | MediaPlayerEntityFeature.PLAY

        # Check for volume control on speaker service
        speaker_service = self._get_speaker_service()
        if speaker_service:
            if speaker_service.has(CharacteristicsTypes.VOLUME):
                features |= MediaPlayerEntityFeature.VOLUME_SET
            if speaker_service.has(CharacteristicsTypes.VOLUME_SELECTOR):
                features |= MediaPlayerEntityFeature.VOLUME_STEP
            if speaker_service.has(CharacteristicsTypes.MUTE):
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

        return clamp_enum_to_char(
            RemoteKeyValues, self.service[CharacteristicsTypes.REMOTE_KEY]
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

    def _get_speaker_service(self) -> Service | None:
        """Get the speaker service linked to this TV."""
        this_accessory = self._accessory.entity_map.aid(self._aid)
        this_tv = this_accessory.services.iid(self._iid)

        speaker_service = this_accessory.services.first(
            service_type=ServicesTypes.SPEAKER,
            parent_service=this_tv,
        )
        return speaker_service

    @property
    def volume_level(self) -> float | None:
        """Return the volume level (0..1)."""
        speaker_service = self._get_speaker_service()
        if not speaker_service or not speaker_service.has(CharacteristicsTypes.VOLUME):
            return None
        
        # HomeKit volume is 0-100, Home Assistant uses 0.0-1.0
        volume = speaker_service.value(CharacteristicsTypes.VOLUME)
        if volume is not None:
            return volume / 100.0
        return None

    @property
    def is_volume_muted(self) -> bool | None:
        """Return boolean if volume is muted."""
        speaker_service = self._get_speaker_service()
        if not speaker_service or not speaker_service.has(CharacteristicsTypes.MUTE):
            return None
        
        return speaker_service.value(CharacteristicsTypes.MUTE)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1."""
        speaker_service = self._get_speaker_service()
        if not speaker_service or not speaker_service.has(CharacteristicsTypes.VOLUME):
            return
        
        # Convert Home Assistant volume (0.0-1.0) to HomeKit volume (0-100)
        homekit_volume = int(volume * 100)
        
        # Build the characteristics update for the speaker service
        chars_to_update = {CharacteristicsTypes.VOLUME: homekit_volume}
        payload = speaker_service.build_update(chars_to_update)
        await self._accessory.put_characteristics(payload)

    async def async_volume_up(self) -> None:
        """Send volume up command."""
        speaker_service = self._get_speaker_service()
        if not speaker_service or not speaker_service.has(
            CharacteristicsTypes.VOLUME_SELECTOR
        ):
            return
        
        # Volume selector: 0 = increment, 1 = decrement
        chars_to_update = {CharacteristicsTypes.VOLUME_SELECTOR: 0}
        payload = speaker_service.build_update(chars_to_update)
        await self._accessory.put_characteristics(payload)

    async def async_volume_down(self) -> None:
        """Send volume down command."""
        speaker_service = self._get_speaker_service()
        if not speaker_service or not speaker_service.has(
            CharacteristicsTypes.VOLUME_SELECTOR
        ):
            return
        
        # Volume selector: 0 = increment, 1 = decrement
        chars_to_update = {CharacteristicsTypes.VOLUME_SELECTOR: 1}
        payload = speaker_service.build_update(chars_to_update)
        await self._accessory.put_characteristics(payload)

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute media player."""
        speaker_service = self._get_speaker_service()
        if not speaker_service or not speaker_service.has(CharacteristicsTypes.MUTE):
            return
        
        chars_to_update = {CharacteristicsTypes.MUTE: mute}
        payload = speaker_service.build_update(chars_to_update)
        await self._accessory.put_characteristics(payload)

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
