"""Support for WoW Blizzard API sensors with all features."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DOMAIN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REGION,
    CONF_CHARACTERS,
    CONF_GAME_VERSION,
    CONF_ENABLE_SERVER_STATUS,
    CONF_ENABLE_PVP,
    CONF_ENABLE_RAIDS,
    CONF_ENABLE_MYTHIC_PLUS,
    GAME_VERSION_RETAIL,
    ALL_SENSOR_TYPES,
    BASIC_SENSOR_TYPES,
    SERVER_SENSOR_TYPES,
    PVP_SENSOR_TYPES,
    RAID_SENSOR_TYPES,
    MYTHICPLUS_SENSOR_TYPES,
    EXTENDED_CHARACTER_SENSOR_TYPES,
    EQUIPMENT_SENSOR_TYPES,
    EQUIPMENT_SLOTS,
    DEFAULT_SCAN_INTERVAL,
    FAST_SCAN_INTERVAL,
    SLOW_SCAN_INTERVAL,
    PVP_BRACKETS,
    CURRENT_RAIDS,
    CLASS_COLORS,
)
from .api_client import WoWBlizzardAPIClient

_LOGGER = logging.getLogger(__name__)


class WoWDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching all WoW data from the API."""

    def __init__(
        self, 
        hass: HomeAssistant, 
        client: WoWBlizzardAPIClient,
        characters: List[Dict[str, str]],
        features: Dict[str, bool]
    ):
        """Initialize."""
        self.client = client
        self.characters = characters
        self.features = features
        self.realms = set(char["realm"] for char in characters)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def _fetch_basic_character_data(self, realm: str, character_name: str) -> Dict[str, Any]:
        """Fetch basic character data plus extended fields (media, spec, status)."""
        try:
            profile = await self.client.get_character_profile(realm, character_name)
            equipment = await self.client.get_character_equipment(realm, character_name)
            achievements = await self.client.get_character_achievements(realm, character_name)
            # Extended endpoints — fetched here so all per-character data is in
            # one place. These all gracefully return {} on 404/403.
            media = await self.client.get_character_media(realm, character_name)
            specializations = await self.client.get_character_specializations(realm, character_name)
            status = await self.client.get_character_status(realm, character_name)

            # Item level from character profile response
            item_level = profile.get("equipped_item_level", 0)

            # Get achievement points
            achievement_points = achievements.get("total_points", 0)

            # Get guild information
            guild_name = None
            if profile.get("guild"):
                guild_name = profile["guild"]["name"]

            # Avatar URL from character-media "assets" array
            avatar_url = None
            for asset in (media.get("assets") or []):
                if asset.get("key") == "avatar":
                    avatar_url = asset.get("value")
                    break

            # Convert last_login_timestamp (Unix epoch ms) to a tz-aware
            # datetime for HA's `timestamp` device_class.
            last_login_dt = None
            ll_ts = profile.get("last_login_timestamp")
            if ll_ts:
                try:
                    last_login_dt = datetime.fromtimestamp(ll_ts / 1000, tz=timezone.utc)
                except (TypeError, ValueError, OSError):
                    last_login_dt = None

            # Spec name & role. Prefer the specializations endpoint, which has
            # more detail; fall back to the profile's active_spec when missing.
            active_spec_name = (
                (specializations.get("active_specialization") or {}).get("name")
                or (profile.get("active_spec") or {}).get("name")
            )
            spec_role = None
            spec_role_obj = (specializations.get("active_specialization") or {}).get("role")
            if isinstance(spec_role_obj, dict):
                spec_role = spec_role_obj.get("type") or spec_role_obj.get("name")
            elif isinstance(spec_role_obj, str):
                spec_role = spec_role_obj

            # Parse equipped items into a per-slot dict keyed by lowercase
            # slot type (e.g. "head", "main_hand").
            equipment_slots: Dict[str, Dict[str, Any]] = {}
            for item in (equipment.get("equipped_items") or []):
                slot_type = (item.get("slot") or {}).get("type")
                if not slot_type:
                    continue
                item_name = item.get("name")
                if isinstance(item_name, dict):
                    item_name = item_name.get("name")
                level_value = (item.get("level") or {}).get("value")
                quality = (item.get("quality") or {}).get("name")
                equipment_slots[slot_type.lower()] = {
                    "name": item_name,
                    "item_level": level_value,
                    "quality": quality,
                }

            # Status endpoint: is_valid is the main interesting field.
            is_valid = status.get("is_valid")

            return {
                "character_level": profile.get("level", 0),
                "character_item_level": item_level,
                "guild_name": guild_name,
                "achievement_points": achievement_points,
                "last_login_timestamp": profile.get("last_login_timestamp"),
                "character_class": profile.get("character_class", {}).get("name"),
                "character_race": profile.get("race", {}).get("name"),
                "realm": profile.get("realm", {}).get("name"),
                "faction": profile.get("faction", {}).get("name"),
                "gender": profile.get("gender", {}).get("name"),
                "spec": active_spec_name,

                # Extended sensors
                "character_avatar": avatar_url,
                "character_id": profile.get("id") or status.get("id"),
                "character_faction": (profile.get("faction") or {}).get("name"),
                "character_race_name": (profile.get("race") or {}).get("name"),
                "character_class_name": (profile.get("character_class") or {}).get("name"),
                "character_active_spec": active_spec_name,
                "character_spec_role": spec_role,
                "character_gender": (profile.get("gender") or {}).get("name"),
                "character_last_login": last_login_dt,
                "character_average_item_level": profile.get("average_item_level"),
                "character_experience": profile.get("experience"),
                "character_is_valid": is_valid,

                # Equipment per-slot (consumed by WoWEquipmentSlotSensor)
                "equipment_slots": equipment_slots,
            }

        except Exception as err:
            _LOGGER.error(f"Error fetching basic data for {character_name}-{realm}: {err}")
            return {}

    async def _fetch_server_data(self, realm: str) -> Dict[str, Any]:
        """Fetch server status data."""
        if not self.features.get(CONF_ENABLE_SERVER_STATUS, False):
            return {}

        try:
            realm_info = await self.client.get_realm_info(realm)
            connected_realm = await self.client.get_connected_realm(realm)

            status = "Unknown"
            population = "Unknown"
            queue_time = 0

            if connected_realm:
                status = connected_realm.get("status", {}).get("name", "Unknown")
                population = connected_realm.get("population", {}).get("name", "Unknown")
                # Get queue information if available
                if connected_realm.get("has_queue"):
                    queue_time = connected_realm.get("queue_time", 0)

            return {
                "realm_status": status,
                "realm_population": population,
                "realm_queue": queue_time,
                "realm_timezone": realm_info.get("timezone", "Unknown"),
                "realm_locale": realm_info.get("locale", "Unknown"),
            }

        except Exception as err:
            _LOGGER.error(f"Error fetching server data for {realm}: {err}")
            return {}

    async def _fetch_pvp_data(self, realm: str, character_name: str) -> Dict[str, Any]:
        """Fetch PvP data for a character."""
        if not self.features.get(CONF_ENABLE_PVP, False):
            return {}

        try:
            pvp_data = await self.client.get_all_pvp_data(realm, character_name)

            # Extract ratings and stats
            ratings_2v2 = 0
            ratings_3v3 = 0
            ratings_rbg = 0
            honor_level = 0
            wins_season = 0

            if pvp_data.get("summary"):
                honor_level = pvp_data["summary"].get("honor_level", 0)

            # Process bracket data
            for bracket, data in pvp_data.items():
                if bracket == "summary":
                    continue
                    
                if not data or "rating" not in data:
                    continue

                rating = data["rating"]
                season_wins = data.get("season_match_statistics", {}).get("won", 0)
                wins_season += season_wins

                if bracket == "2v2":
                    ratings_2v2 = rating
                elif bracket == "3v3":
                    ratings_3v3 = rating
                elif bracket == "rbg":
                    ratings_rbg = rating

            return {
                "pvp_2v2_rating": ratings_2v2,
                "pvp_3v3_rating": ratings_3v3,
                "pvp_rbg_rating": ratings_rbg,
                "pvp_honor_level": honor_level,
                "pvp_wins_season": wins_season,
            }

        except Exception as err:
            _LOGGER.error(f"Error fetching PvP data for {character_name}-{realm}: {err}")
            return {}

    async def _fetch_raid_data(self, realm: str, character_name: str) -> Dict[str, Any]:
        """Fetch raid progress data."""
        if not self.features.get(CONF_ENABLE_RAIDS, False):
            return {}

        try:
            encounters = await self.client.get_character_encounters_raids(realm, character_name)

            progress_lfr = 0
            progress_normal = 0
            progress_heroic = 0
            progress_mythic = 0
            total_kills = 0

            # Count all boss kills from all expansions
            if encounters and "expansions" in encounters:
                for expansion in encounters["expansions"]:
                    for instance in expansion.get("instances", []):
                        for mode in instance.get("modes", []):
                            difficulty = mode.get("difficulty", {}).get("name", "")
                            progress = mode.get("progress", {})
                            completed = progress.get("completed_count", 0)
                            total_encounters = progress.get("total_count", 0)

                            if "raid finder" in difficulty.lower():
                                progress_lfr += completed
                            elif "normal" in difficulty.lower():
                                progress_normal += completed
                            elif "heroic" in difficulty.lower():
                                progress_heroic += completed
                            elif "mythic" in difficulty.lower():
                                progress_mythic += completed

                            total_kills += completed

            return {
                "raid_progress_lfr": progress_lfr,
                "raid_progress_normal": progress_normal,
                "raid_progress_heroic": progress_heroic,
                "raid_progress_mythic": progress_mythic,
                "raid_kills_total": total_kills,
            }

        except Exception as err:
            _LOGGER.error(f"Error fetching raid data for {character_name}-{realm}: {err}")
            return {}

    async def _fetch_mythicplus_data(self, realm: str, character_name: str) -> Dict[str, Any]:
        """Fetch Mythic+ data."""
        if not self.features.get(CONF_ENABLE_MYTHIC_PLUS, False):
            return {}

        try:
            profile = await self.client.get_character_mythicplus_profile(realm, character_name)
            season_data = await self.client.get_character_mythicplus_season(realm, character_name)

            score = 0
            best_run = 0
            runs_completed = 0
            runs_timed = 0
            weekly_best = 0

            # Get current season data
            if season_data:
                best_runs = season_data.get("best_runs", [])
                all_runs = []
                # Collect all runs from all dungeons
                for run in best_runs:
                    if "members" in run:
                        all_runs.append(run)
                # If the API provides additional fields for all runs, add here
                if best_runs:
                    best_run = max(run.get("keystone_level", 0) for run in best_runs)
                # Total number of all runs and timed runs
                runs_completed = len(all_runs)
                runs_timed = sum(1 for run in all_runs if run.get("is_completed_within_time", False))

                # Use Blizzard score directly
                score = season_data.get("mythic_rating", {}).get("rating", 0)

            # Get weekly data if available
            if profile and "current_period" in profile:
                current_period = profile["current_period"]
                if "best_runs" in current_period:
                    weekly_runs = current_period["best_runs"]
                    if weekly_runs:
                        weekly_best = max(run.get("keystone_level", 0) for run in weekly_runs)

            return {
                "mythicplus_score": score,
                "mythicplus_best_run": best_run,
                "mythicplus_runs_completed": runs_completed,
                "mythicplus_runs_timed": runs_timed,
                "mythicplus_weekly_best": weekly_best,
            }

        except Exception as err:
            _LOGGER.error(f"Error fetching M+ data for {character_name}-{realm}: {err}")
            return {}

    async def _async_update_data(self):
        """Update data via library."""
        try:
            all_data = {}
            
            # Fetch data for each character
            for character in self.characters:
                realm = character["realm"]
                name = character["character_name"]
                char_key = f"{realm}-{name}"
                
                # Fetch all character data
                basic_data = await self._fetch_basic_character_data(realm, name)
                pvp_data = await self._fetch_pvp_data(realm, name)
                raid_data = await self._fetch_raid_data(realm, name)
                mythicplus_data = await self._fetch_mythicplus_data(realm, name)
                
                # Combine all character data
                character_data = {
                    **basic_data,
                    **pvp_data,
                    **raid_data,
                    **mythicplus_data,
                }
                
                all_data[char_key] = character_data
                
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.1)

            # Fetch server data for each unique realm
            server_data = {}
            for realm in self.realms:
                realm_data = await self._fetch_server_data(realm)
                server_data[realm] = realm_data
                await asyncio.sleep(0.1)

            # Combine character and server data
            all_data["servers"] = server_data
            all_data["last_update"] = self.last_update_success

            return all_data

        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up WoW Blizzard sensors based on a config entry."""
    client_id = entry.data[CONF_CLIENT_ID]
    client_secret = entry.data[CONF_CLIENT_SECRET]
    region = entry.data[CONF_REGION]
    game_version = entry.data.get(CONF_GAME_VERSION, GAME_VERSION_RETAIL)
    characters = entry.data.get(CONF_CHARACTERS, [])

    is_retail = game_version == GAME_VERSION_RETAIL

    # Feature flags. Classic does not expose Mythic+ or modern raid
    # progress endpoints, so force those off regardless of saved preference.
    features = {
        CONF_ENABLE_SERVER_STATUS: entry.data.get(CONF_ENABLE_SERVER_STATUS, True),
        CONF_ENABLE_PVP: entry.data.get(CONF_ENABLE_PVP, True),
        CONF_ENABLE_RAIDS: entry.data.get(CONF_ENABLE_RAIDS, True) and is_retail,
        CONF_ENABLE_MYTHIC_PLUS: entry.data.get(CONF_ENABLE_MYTHIC_PLUS, True) and is_retail,
    }

    if not characters:
        _LOGGER.error("No characters configured")
        return

    client = WoWBlizzardAPIClient(client_id, client_secret, region, game_version=game_version)
    coordinator = WoWDataUpdateCoordinator(hass, client, characters, features)

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Create sensors
    entities = []
    
    # Character sensors
    for character in characters:
        realm = character["realm"]
        name = character["character_name"]
        char_key = f"{realm}-{name}"
        
        # Basic character sensors (always enabled)
        for sensor_type in BASIC_SENSOR_TYPES:
            entities.append(
                WoWCharacterSensor(coordinator, sensor_type, char_key, name, realm)
            )

        # Extended character sensors (avatar, faction, race, spec, etc.) —
        # available on Retail and Classic. Fields the API does not return
        # for the selected game version resolve to None / unavailable.
        name_slug = name.lower()
        realm_slug = realm.lower().replace(" ", "_").replace("'", "")
        for sensor_type in EXTENDED_CHARACTER_SENSOR_TYPES:
            suggested = None
            if sensor_type == "character_avatar":
                # Task spec requires sensor.{character}_{realm}_avatar
                suggested = f"{name_slug}_{realm_slug}_avatar"
            entities.append(
                WoWCharacterSensor(
                    coordinator, sensor_type, char_key, name, realm,
                    suggested_object_id=suggested,
                )
            )

        # Per-slot equipment sensors
        for slot in EQUIPMENT_SLOTS:
            entities.append(
                WoWEquipmentSlotSensor(coordinator, slot, char_key, name, realm)
            )

        # PvP sensors
        if features[CONF_ENABLE_PVP]:
            for sensor_type in PVP_SENSOR_TYPES:
                entities.append(
                    WoWCharacterSensor(coordinator, sensor_type, char_key, name, realm)
                )
        
        # Raid sensors
        if features[CONF_ENABLE_RAIDS]:
            for sensor_type in RAID_SENSOR_TYPES:
                entities.append(
                    WoWCharacterSensor(coordinator, sensor_type, char_key, name, realm)
                )
        
        # Mythic+ sensors
        if features[CONF_ENABLE_MYTHIC_PLUS]:
            for sensor_type in MYTHICPLUS_SENSOR_TYPES:
                entities.append(
                    WoWCharacterSensor(coordinator, sensor_type, char_key, name, realm)
                )

    # Server sensors
    if features[CONF_ENABLE_SERVER_STATUS]:
        realms = set(char["realm"] for char in characters)
        for realm in realms:
            for sensor_type in SERVER_SENSOR_TYPES:
                entities.append(
                    WoWServerSensor(coordinator, sensor_type, realm)
                )

    async_add_entities(entities)


class WoWCharacterSensor(CoordinatorEntity, SensorEntity):
    """Representation of a WoW character sensor."""

    def __init__(
        self,
        coordinator: WoWDataUpdateCoordinator,
        sensor_type: str,
        char_key: str,
        character_name: str,
        realm: str,
        suggested_object_id: Optional[str] = None,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._sensor_type = sensor_type
        self._char_key = char_key
        self._character_name = character_name
        self._realm = realm

        sensor_config = ALL_SENSOR_TYPES[sensor_type]

        self._attr_name = f"{character_name} {sensor_config['name']}"
        self._attr_unique_id = f"{DOMAIN}_{realm}_{character_name}_{sensor_type}"
        self._attr_icon = sensor_config["icon"]
        self._attr_native_unit_of_measurement = sensor_config.get("unit")
        self._attr_device_class = sensor_config.get("device_class")
        if suggested_object_id:
            self._attr_suggested_object_id = suggested_object_id

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if not self.coordinator.data or self._char_key not in self.coordinator.data:
            return None
        return self.coordinator.data[self._char_key].get(self._sensor_type)

    @property
    def extra_state_attributes(self):
        """Return additional state attributes."""
        if not self.coordinator.data or self._char_key not in self.coordinator.data:
            return {}
        
        char_data = self.coordinator.data[self._char_key]
        
        attributes = {
            "character_name": self._character_name,
            "realm": self._realm,
            "character_class": char_data.get("character_class"),
            "character_race": char_data.get("character_race"),
            "character_level": char_data.get("character_level"),
            "last_update": self.coordinator.last_update_success,
            "faction": char_data.get("faction"),
            "active_spec": char_data.get("spec"),
        }
        
        # Add class color if available
        if char_data.get("character_class") in CLASS_COLORS:
            attributes["class_color"] = CLASS_COLORS[char_data["character_class"]]
        
        # Add specific attributes based on sensor type
        if self._sensor_type in PVP_SENSOR_TYPES:
            attributes["category"] = "pvp"
        elif self._sensor_type in RAID_SENSOR_TYPES:
            attributes["category"] = "raid"
        elif self._sensor_type in MYTHICPLUS_SENSOR_TYPES:
            attributes["category"] = "mythic_plus"
        elif self._sensor_type in EXTENDED_CHARACTER_SENSOR_TYPES:
            attributes["category"] = "character_extended"
        else:
            attributes["category"] = "character"
            
        return attributes

    @property
    def device_info(self):
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, f"{self._realm}_{self._character_name}")},
            "name": f"{self._character_name} ({self._realm})",
            "manufacturer": "Blizzard Entertainment",
            "model": "World of Warcraft Character",
            "sw_version": "The War Within",
        }


class WoWServerSensor(CoordinatorEntity, SensorEntity):
    """Representation of a WoW server sensor."""

    def __init__(
        self, 
        coordinator: WoWDataUpdateCoordinator,
        sensor_type: str,
        realm: str
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._sensor_type = sensor_type
        self._realm = realm
        
        sensor_config = ALL_SENSOR_TYPES[sensor_type]
        
        self._attr_name = f"{realm.title()} {sensor_config['name']}"
        self._attr_unique_id = f"{DOMAIN}_server_{realm}_{sensor_type}"
        self._attr_icon = sensor_config["icon"]
        self._attr_native_unit_of_measurement = sensor_config.get("unit")
        self._attr_device_class = sensor_config.get("device_class")

    @property
    def native_value(self):
        """Return the state of the sensor."""
        if (not self.coordinator.data 
            or "servers" not in self.coordinator.data 
            or self._realm not in self.coordinator.data["servers"]):
            return None
        return self.coordinator.data["servers"][self._realm].get(self._sensor_type)

    @property
    def extra_state_attributes(self):
        """Return additional state attributes."""
        if (not self.coordinator.data 
            or "servers" not in self.coordinator.data 
            or self._realm not in self.coordinator.data["servers"]):
            return {}
        
        realm_data = self.coordinator.data["servers"][self._realm]
        
        return {
            "realm": self._realm,
            "category": "server",
            "timezone": realm_data.get("realm_timezone"),
            "locale": realm_data.get("realm_locale"),
            "last_update": self.coordinator.last_update_success,
        }

    @property
    def device_info(self):
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, f"server_{self._realm}")},
            "name": f"{self._realm.title()} Server",
            "manufacturer": "Blizzard Entertainment",
            "model": "World of Warcraft Realm",
            "sw_version": "The War Within",
        }


class WoWEquipmentSlotSensor(CoordinatorEntity, SensorEntity):
    """Representation of a single equipment slot on a character.

    State is the equipped item's name; the item level is exposed as an
    ``item_level`` attribute. Empty slots resolve to ``None`` (unavailable).
    """

    def __init__(
        self,
        coordinator: WoWDataUpdateCoordinator,
        slot: str,
        char_key: str,
        character_name: str,
        realm: str,
    ):
        super().__init__(coordinator)
        self._slot = slot  # canonical uppercase, e.g. "HEAD"
        self._slot_key = slot.lower()
        self._char_key = char_key
        self._character_name = character_name
        self._realm = realm

        slot_label = slot.replace("_", " ").title()
        self._attr_name = f"{character_name} {slot_label} Slot"
        self._attr_unique_id = f"{DOMAIN}_{realm}_{character_name}_slot_{self._slot_key}"
        self._attr_icon = "mdi:tshirt-crew"

        # Task spec: sensor.{character}_{realm}_slot_{slot_lowercase}
        name_slug = character_name.lower()
        realm_slug = realm.lower().replace(" ", "_").replace("'", "")
        self._attr_suggested_object_id = f"{name_slug}_{realm_slug}_slot_{self._slot_key}"

    def _slot_data(self) -> Dict[str, Any]:
        if not self.coordinator.data or self._char_key not in self.coordinator.data:
            return {}
        slots = (self.coordinator.data[self._char_key] or {}).get("equipment_slots") or {}
        return slots.get(self._slot_key) or {}

    @property
    def native_value(self):
        """Return the equipped item's name, or None if the slot is empty."""
        item = self._slot_data()
        name = item.get("name")
        return name if name else None

    @property
    def available(self) -> bool:
        """Mark unavailable if the slot is empty or coordinator has no data."""
        return bool(self.native_value)

    @property
    def extra_state_attributes(self):
        item = self._slot_data()
        char_data = (self.coordinator.data or {}).get(self._char_key) or {}
        return {
            "slot": self._slot,
            "item_level": item.get("item_level"),
            "quality": item.get("quality"),
            "character_name": self._character_name,
            "realm": self._realm,
            "character_class": char_data.get("character_class"),
            "category": "equipment",
            "last_update": self.coordinator.last_update_success,
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{self._realm}_{self._character_name}")},
            "name": f"{self._character_name} ({self._realm})",
            "manufacturer": "Blizzard Entertainment",
            "model": "World of Warcraft Character",
        }