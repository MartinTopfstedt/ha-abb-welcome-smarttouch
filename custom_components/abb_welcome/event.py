"""Event entities for ABB Welcome intercom events.

* ``Intercom`` fires HA events when the portal reports rings, door opens,
  calls, etc.  These show up in the HA logbook and can trigger automations.
* One ``<door> Doorbell`` entity per outdoor station fires ``ring`` the
  moment the realtime SIP listener sees that station's INVITE.  It carries
  the doorbell device class, so HomeKit Bridge can link it to the camera
  and announce rings.
"""

from __future__ import annotations

import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_RING
from .coordinator import ABBWelcomeCoordinator, ABBWelcomeData

_LOGGER = logging.getLogger(__name__)

EVENT_TYPES = [
    "ring",
    "door-open",
    "call-answered",
    "call-terminated",
    "call-missed",
    "light",
    "screenshot",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ABB Welcome event entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    gateway_uuid = entry.data.get("gateway_uuid", "unknown")
    doors = entry.data.get("doors", []) or []
    entities: list[EventEntity] = []

    coordinator: ABBWelcomeCoordinator = data.get("coordinator")
    if coordinator and coordinator.has_certs:
        entities.append(ABBWelcomeEventEntity(coordinator, gateway_uuid, doors))

    # Doorbell entities depend on the realtime SIP listener for rings.
    if "sip_listener" in data:
        for door in doors:
            station_id = str(door.get("station_id", "")).strip()
            if station_id:
                entities.append(
                    ABBWelcomeDoorbellEvent(
                        gateway_uuid,
                        station_id,
                        str(door.get("name") or station_id),
                    )
                )

    async_add_entities(entities)


def _gateway_device_info(gateway_uuid: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, gateway_uuid)},
        name="ABB Welcome Gateway",
        manufacturer="ABB / Busch-Jaeger",
        model="IP Gateway (MRANGE)",
    )


class ABBWelcomeDoorbellEvent(EventEntity):
    """Doorbell press for one outdoor station, from the realtime SIP ring."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]

    def __init__(self, gateway_uuid: str, station_id: str, door_name: str) -> None:
        self._station_id = station_id
        self._attr_name = f"{door_name} Doorbell"
        self._attr_unique_id = f"{gateway_uuid}_doorbell_{station_id}"
        self._attr_device_info = _gateway_device_info(gateway_uuid)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_RING, self._handle_ring)
        )

    @callback
    def _handle_ring(self, event: Event) -> None:
        if str(event.data.get("station_id", "")).strip() != self._station_id:
            return
        self._trigger_event("ring", dict(event.data))
        self.async_write_ha_state()


class ABBWelcomeEventEntity(EventEntity):
    """Event entity that fires on intercom activity."""

    _attr_has_entity_name = True
    _attr_name = "Intercom"
    _attr_icon = "mdi:bell-ring"
    _attr_event_types = EVENT_TYPES

    def __init__(
        self,
        coordinator: ABBWelcomeCoordinator,
        gateway_uuid: str,
        doors: list[dict],
    ) -> None:
        self._coordinator = coordinator
        self._station_names = {
            str(door.get("station_id", "")).strip(): str(
                door.get("name") or door.get("station_id") or ""
            )
            for door in doors
            if str(door.get("station_id", "")).strip()
        }
        self._attr_unique_id = f"{gateway_uuid}_intercom_events"
        self._attr_device_info = _gateway_device_info(gateway_uuid)
        self._last_seen_id = ""
        self._seeded = False

    async def async_added_to_hass(self) -> None:
        # The coordinator keeps no history across restarts: the first poll
        # returns the portal's recent events, which are history, not new
        # activity.  Take whatever is already loaded as the baseline.
        self._seed_from(self._coordinator.data)
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_update)
        )

    def _seed_from(self, data: ABBWelcomeData | None) -> None:
        if data and data.events:
            self._last_seen_id = data.events[0].event_id
            self._seeded = True

    @callback
    def _handle_update(self) -> None:
        data = self._coordinator.data
        if not data or not data.events:
            return

        if not self._seeded:
            # First data after the entity was added (initial poll was still
            # pending or failed): baseline it without firing.
            self._seed_from(data)
            return

        new_events = []
        for evt in data.events:
            if evt.event_id == self._last_seen_id:
                break
            new_events.append(evt)
        self._last_seen_id = data.events[0].event_id

        # Events are newest first; fire oldest first so the entity ends on
        # the newest event.
        for evt in reversed(new_events):
            if evt.event_type == "screenshot":
                continue

            station = evt.local_name or self._station_names.get(evt.station_id, "")
            label = evt.event_type.replace("-", " ").title()

            self._trigger_event(
                evt.event_type,
                {
                    "event_type": evt.event_type,
                    "event_label": label,
                    "station": station,
                    "station_name": station,
                    "station_id": evt.station_id,
                    "local_id": evt.local_id,
                    "local_name": evt.local_name,
                    "sender": evt.sender,
                    "belongs_to": evt.belongs_to,
                    "timestamp": evt.timestamp,
                    "event_id": evt.event_id,
                },
            )
            # _trigger_event does not write state.  Write after each one so
            # every event gets its own state change (logbook, recorder,
            # event triggers), not just the last of the poll.
            self.async_write_ha_state()
