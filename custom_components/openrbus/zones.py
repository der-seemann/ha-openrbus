"""Evidence-backed OpenRBus zone classification and presentation.

Zone objects in the BDR catalogue are CANopen arrays.  Their subindex is a
stable bus identity, while the function configured in CP020 is the only
reliable indicator of whether that slot is in use and what it controls.  Do
not guess a zone from a node number or from a static device family.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from openrbus.protocol.canip import ObjectAddress
from openrbus.registry import Registry

ZONE_FUNCTION_INDEX = 0x3404  # CP020, Zone Function
ZONE_FRIENDLY_NAME_INDEX = 0x340F
ZONE_SHORT_NAME_INDEX = 0x3410
_REGISTRY = Registry.load_default()
_ZONE_FUNCTION_DEFINITION = _REGISTRY.find(ObjectAddress(ZONE_FUNCTION_INDEX, 0))
_ZONE_FUNCTION_ENUM = next(
    (
        item
        for item in _REGISTRY.enums
        if _ZONE_FUNCTION_DEFINITION is not None
        and item.name == _ZONE_FUNCTION_DEFINITION.wire.enum_name
    ),
    None,
)


class ZoneKind(StrEnum):
    """User-facing grouping inferred from the manufacturer ZoneFunction enum."""

    GENERAL = "general"
    HEATING = "heating"
    DHW = "dhw"
    SOLAR = "solar"
    INACTIVE = "inactive"
    OTHER = "other"
    UNKNOWN = "unknown"


class ZoneAssociation(StrEnum):
    """How an exact object projection relates to a CP020 slot."""

    NOT_ZONE = "not_zone"
    PARENT = "parent"
    FUNCTION_SELECTOR = "function_selector"
    ZONE_SLOT = "zone_slot"
    UNRESOLVED = "unresolved"


_INACTIVE_FUNCTIONS = frozenset({0})
_DHW_FUNCTIONS = frozenset({6, 7, 10, 11, 12, 13, 31})
# Direct/mixing/high-temperature/fan-convector/BSB are heating circuits.  The
# Values are from Core's localized manufacturer ZoneFunction enum.  Remaining
# known enabled values are neither heating nor DHW; they stay separate.
_HEATING_FUNCTIONS = frozenset({1, 2, 4, 5, 200})

# Exact candidate inventory joined by address to Core v0.4.6 and manufacturer
# OBD 1.47. These identifiers are provenance only, never parsed at runtime.
# Matching ten-item array bounds do not establish that an item's subindex is a
# CP020 slot (for example, CP081 is a user-activity dimension). Keep the
# inventory separate from the small set with direct slot semantics.
ZONE_OBJECT_CANDIDATE_SOURCES: Mapping[int, str] = MappingProxyType(
    {
        0x3401: "parZoneTFlowSetpointMax",
        0x3402: "parZoneTFlowSetpoint",
        0x3404: "parZoneFunction",
        0x3405: "parZoneMixingValveBandwith",
        0x3406: "parZoneEnableRubCommunication",
        0x3408: "parZonePumpPostRun",
        0x3409: "parZoneMixingValveShift",
        0x340A: "parZoneAmbiantHolidaySetpoint",
        0x340B: "parZoneAmbiantNightSetpoint",
        0x340D: "parZoneFriendlyNameUserActivity",
        0x340E: "parZoneToutsideExternalSelector",
        0x340F: "parZoneFriendlyName",
        0x3410: "parZoneFriendlyNameShort",
        0x3411: "parZoneFriendlyNameCoolingSetpoint",
        0x3413: "parZoneRoomManualSetpoint",
        0x3414: "parZoneHCZPD",
        0x3415: "parZoneHCZPN",
        0x3416: "parZoneSlope",
        0x3417: "parZoneRoomUnitInfluence",
        0x3418: "parZoneRoomSensorCalibration",
        0x3419: "parZoneTFlowSetpointMin",
        0x341A: "parZoneTFlowCoolingMixingSetpoint",
        0x341B: "parZoneTFlowCoolingFanSetpoint",
        0x341C: "parZonePumpOutputConfiguration",
        0x341D: "parZoneAnticipation",
        0x341E: "parZoneAutoAdapt",
        0x341F: "parZoneMode",
        0x3420: "parZoneMixingValveOpeningTime",
        0x3421: "parZoneStartTimeHoliday",
        0x3422: "parZoneEndTimeHoliday",
        0x3423: "parZoneEndTimeModeChange",
        0x3424: "parZoneReducedNightMode",
        0x3425: "parZoneDhwComfortSetpoint",
        0x3426: "parZoneDhwReducedSetpoint",
        0x3427: "parZoneDhwHolidaySetpoint",
        0x3428: "parZoneDhwAntilegionelSetpoint",
        0x3429: "parZoneDhwStartTimeAntilegionel",
        0x342A: "parZoneDhwDurationTimeAntilegionel",
        0x342B: "parZoneThermostatEnabled",
        0x342C: "parZoneDhwHysterisis",
        0x342D: "parZoneDhwOptimise",
        0x342E: "parZoneDhwRelease",
        0x3430: "parZoneDhwPriority",
        0x3431: "parZoneTimeProgramMonday1",
        0x3432: "parZoneTimeProgramTuesday1",
        0x3433: "parZoneTimeProgramWednesday1",
        0x3434: "parZoneTimeProgramThursday1",
        0x3435: "parZoneTimeProgramFriday1",
        0x3436: "parZoneTimeProgramSaturday1",
        0x3437: "parZoneTimeProgramSunday1",
        0x3438: "parZoneTimeProgramMonday2",
        0x3439: "parZoneTimeProgramTuesday2",
        0x343A: "parZoneTimeProgramWednesday2",
        0x343B: "parZoneTimeProgramThursday2",
        0x343C: "parZoneTimeProgramFriday2",
        0x343D: "parZoneTimeProgramSaturday2",
        0x343E: "parZoneTimeProgramSunday2",
        0x343F: "parZoneTimeProgramMonday3",
        0x3440: "parZoneTimeProgramTuesday3",
        0x3441: "parZoneTimeProgramWednesday3",
        0x3442: "parZoneTimeProgramThursday3",
        0x3443: "parZoneTimeProgramFriday3",
        0x3444: "parZoneTimeProgramSaturday3",
        0x3445: "parZoneTimeProgramSunday3",
        0x3446: "parZoneTimeProgramMonday4",
        0x3447: "parZoneTimeProgramTuesday4",
        0x3448: "parZoneTimeProgramWednesday4",
        0x3449: "parZoneTimeProgramThursday4",
        0x344A: "parZoneTimeProgramFriday4",
        0x344B: "parZoneTimeProgramSaturday4",
        0x344C: "parZoneTimeProgramSunday4",
        0x344D: "parZoneScreedDrying",
        0x344E: "parZoneStartDryingTemp",
        0x344F: "parZoneStopDryingTemp",
        0x3450: "parZoneFlowSensorEnabled",
        0x3451: "parZoneTemporaryRoomSetpoint",
        0x3452: "parZonePowerSetpoint",
        0x3453: "parZonePwmPumpSpeed",
        0x3454: "parZoneSwimmingPoolSetpoint",
        0x3455: "parZoneFirePlaceEnabled",
        0x3456: "parZoneDhwTypeAntilegionel",
        0x3458: "parZoneTimeProgramSelected",
        0x345B: "parZoneProcessHeatSetpoint",
        0x345C: "parZoneProcessHeatHysterisis",
        0x345D: "parZoneProcessHeatOffset",
        0x345E: "parZoneDhwStartDayAntilegionel",
        0x345F: "parZoneOthLogicLevelContact",
        0x3460: "parZoneAmbiantCoolingNightSetpoint",
        0x3461: "parZoneThermostatLogicLevel",
        0x3462: "parZoneGasProcessMinTemperature010V",
        0x3463: "parZoneIconType",
        0x3464: "parZoneLinkedRoomUnit",
        0x3465: "parZoneRoomUnitBusChannel",
        0x3466: "parZoneOthContactReversedForCooling",
        0x3467: "parZoneDhwCalorifierOffset",
        0x3468: "parZoneDhwCalorifierSetpointRaise",
        0x3469: "parZoneProcessHeatCalorifierSetpointRaise",
        0x346A: "parZoneHeatUpSpeed",
        0x346B: "parZoneCoolDownSpeed",
        0x346C: "parZoneMaxPreHeatTime",
        0x346D: "parZoneEnableMixingValveSmartAlgorithm",
        0x346E: "parZoneDhwFlowDetectFilterTau",
        0x346F: "parZoneDhwTasEnabled",
        0x3470: "parZoneBuffered",
        0x3471: "parZoneHeatingControlStrategy",
        0x3472: "parZoneDhwTankVolume",
        0x3473: "parZoneCommercialDhwMode",
        0x3474: "parZoneDhwElectricalBackupCapacity",
        0x3475: "parZoneDhwProductionTime",
        0x3476: "parZonePumpDeltaOn",
        0x3477: "parZonePumpDeltaOff",
        0x3478: "parZoneCirculationFunction",
        0x3479: "parZoneCirculationOffset",
        0x347A: "parZoneCirculationHysteresis",
        0x347B: "parZoneCirculationDuration",
        0x347C: "parZoneCirculationFlow",
        0x347D: "parZoneHydraulicBalancingCapability",
        0x347E: "parZoneGasProcessMaxTemperature010V",
        0x347F: "parZoneGasProcessMinVoltage010V",
        0x3480: "parZoneGasProcessMaxVoltage010V",
        0x3481: "parZonePumpType",
        0x3482: "parZoneGasProcessHeatStepErrorDenominator",
        0x3483: "parZoneScreedDryingDuration1",
        0x3484: "parZoneScreedDryingStartTemp1",
        0x3485: "parZoneScreedDryingEndTemp1",
        0x3486: "parZoneScreedDryingDuration2",
        0x3487: "parZoneScreedDryingStartTemp2",
        0x3488: "parZoneScreedDryingEndTemp2",
        0x3489: "parZoneScreedDryingDuration3",
        0x348A: "parZoneScreedDryingStartTemp3",
        0x348B: "parZoneScreedDryingEndTemp3",
        0x348C: "parZoneScreedDryingEnable",
        0x348D: "parZonePumpLinControlMode",
        0x348F: "parZoneDhwMaxTimeAntilegionella",
        0x3490: "parZoneDhwAntiLegionellaErrorEnabled",
        0x3491: "parZoneDhwAntiLegionellaErrorNumberOfAttempts",
        0x363D: "parZoneDHWExternalLoadType",
        0x363E: "parZoneDHWPrimaryTimeProgramMonday1",
        0x363F: "parZoneDHWPrimaryTimeProgramTuesday1",
        0x3640: "parZoneDHWPrimaryTimeProgramWednesday1",
        0x3641: "parZoneDHWPrimaryTimeProgramThursday1",
        0x3642: "parZoneDHWPrimaryTimeProgramFriday1",
        0x3643: "parZoneDHWPrimaryTimeProgramSaturday1",
        0x3644: "parZoneDHWPrimaryTimeProgramSunday1",
        0x3645: "parZoneDHWPrimaryTimeProgramMonday2",
        0x3646: "parZoneDHWPrimaryTimeProgramTuesday2",
        0x3647: "parZoneDHWPrimaryTimeProgramWednesday2",
        0x3648: "parZoneDHWPrimaryTimeProgramThursday2",
        0x3649: "parZoneDHWPrimaryTimeProgramFriday2",
        0x364A: "parZoneDHWPrimaryTimeProgramSaturday2",
        0x364B: "parZoneDHWPrimaryTimeProgramSunday2",
        0x364C: "parZoneDHWPrimaryTimeProgramMonday3",
        0x364D: "parZoneDHWPrimaryTimeProgramTuesday3",
        0x364E: "parZoneDHWPrimaryTimeProgramWednesday3",
        0x364F: "parZoneDHWPrimaryTimeProgramThursday3",
        0x3650: "parZoneDHWPrimaryTimeProgramFriday3",
        0x3651: "parZoneDHWPrimaryTimeProgramSaturday3",
        0x3652: "parZoneDHWPrimaryTimeProgramSunday3",
        0x3653: "parZoneDHWPrimaryTimeProgramSelected",
        0x3654: "parZoneDhwPrimaryComfortSetpoint",
        0x3655: "parZoneDHWPrimaryReducedSetpoint",
        0x3656: "parZoneDHWPrimaryDelayGeneratorStart",
        0x3657: "parZoneDHWPrimaryDelayGeneratorStop",
        0x3658: "parZoneDHWPrimaryTempoBetweenStages",
        0x3659: "parZoneDHWPrimaryHysteresis",
        0x365A: "parZoneDHWPrimaryTOffset",
        0x365B: "parZoneDHWPrimaryLoadType",
        0x365C: "parZoneDHWPrimaryBoilerSwitch",
        0x365D: "parZoneDHWprimaryAntiLegionellaSetpoint",
        0x365E: "parZoneDHWPrimaryStartTimeHoliday",
        0x365F: "parZoneDHWPrimaryEndTimeHoliday",
        0x3660: "parZoneDHWPrimaryEndTimeModeChange",
        0x3661: "parZoneDHWPrimaryMode",
        0x3665: "parZoneDHWPrimaryPumpPostRun",
        0x3666: "parZoneDHWPrimaryLoadAfterDhwTime",
        0x3667: "parZoneDHWPrimaryLoadAfterChTime",
        0x3668: "parZoneDhwPrimaryCombiFlowHigh",
        0x3669: "parZoneDhwPrimaryCombiDeltaFlow",
        0x366A: "parZoneDhwPrimaryCombiIFlowLow",
        0x366B: "parZoneDhwPrimaryCombiAlphaDhw",
        0x366C: "parZoneDhwPrimaryCombiPumpSetpoint",
        0x366D: "parZoneDhwPrimaryCombiAlphaPump",
        0x366E: "parZoneDhwPrimaryCombiDtProducerSetpointMax",
        0x366F: "parZoneDhwPrimaryCombiTempoProducerSetpoint",
        0x3670: "parZoneDhwPrimaryCombiD",
        0x3675: "parZoneDhwPrimaryHolidaySetpoint",
        0x3676: "parZoneDhwPrimaryMk1CombiMode",
        0x3677: "parZoneDhwPrimaryShowerTimerTime",
        0x3678: "parZoneDhwPrimaryShowerTimerAction",
        0x3679: "parZoneDhwPrimaryShowerTimerTemperatureReduced",
        0x3680: "parZoneDhwPrimaryDurationTimeAntilegionella",
        0x3681: "parZoneDhwPrimaryMaxTimeAntilegionella",
        0x3683: "parZoneDhwPrimaryTankAutoEcoMode",
        0x3686: "parZoneDhwPrimaryStartDayAntiLegionella",
        0x3687: "parZoneDhwPrimaryStartTimeAntiLegionella",
        0x3690: "parZoneDhwPrimaryPvSetpoint",
        0x3691: "parZoneDhwPrimaryShowerVolume",
        0x3699: "parZoneDhwPrimaryScfMode",
        0x369A: "parZoneDhwPrimaryScfHysteresis",
        0x369B: "parZoneDhwPrimaryScfSetpoint",
        0x36AA: "parZoneDhwPrimaryAntiLegionellaErrorEnabled",
        0x36AB: "parZoneDhwPrimaryAntiLegionellaErrorNumberOfAttempts",
        0x5402: "varZoneMvdClosing",
        0x5403: "varZoneMvdOpening",
        0x5404: "varZoneTRoom",
        0x5405: "varZoneTflow",
        0x5406: "varZonePumpRunning",
        0x5407: "varZonePumpSpeed",
        0x5408: "varZoneTemperatureSetpoint",
        0x5409: "varZoneModulationSetpoint",
        0x540A: "varZoneTflowAverage",
        0x540E: "varZoneWinningHeatDemand",
        0x540F: "varZoneTroomTemporarySetpoint",
        0x5410: "varZoneCurrentMode",
        0x5411: "varZoneThermostatStatus",
        0x5412: "varZoneHardwareName",
        0x5413: "varZoneCurrentActivities",
        0x5414: "varZoneOtPresent",
        0x5415: "varZoneHdOnOffDemand",
        0x5416: "varZoneModulatedHeatDemandPresent",
        0x5417: "varZoneOtSmartPowerAvailable",
        0x5418: "varZoneRuPresent",
        0x5419: "varZoneTRoomSetpoint",
        0x541B: "varZoneCtrPumpStarts",
        0x541C: "varZoneConfigurationBitfield",
        0x541D: "varZoneCurrentHeatingMode",
        0x541F: "varZoneOverHeatActive",
        0x5420: "varZoneWinningHeatDemandTypeRequested",
        0x5423: "varZoneHeatDemandCommandBitfield",
        0x5424: "varZoneManagerSystemFlowTemperature",
        0x5427: "varZoneSpecialMode",
        0x5429: "varZoneManagerSystemPower",
        0x542B: "varZoneSystemPower",
        0x542D: "varZoneSystemReturnTemp",
        0x542E: "varZoneTOutside",
        0x542F: "varZoneToutsideAverageShortWindon",
        0x5430: "varZoneToutsideAverageLongWindow",
        0x5431: "varZoneToutsideConnected",
        0x5432: "varZoneDiscoveryTable",
        0x5433: "varZoneTDhwTopTemperature",
        0x5434: "varZoneRoomTemperatureMeasured",
        0x5436: "varZoneCalculatedRoomTemperatureSetpoint",
        0x5437: "varZoneSecondarySwimmingPoolpumpStatus",
        0x5438: "varZoneElectricalBackupOutputStatus",
        0x5439: "varZoneTreturn",
        0x543A: "varZoneTBuffer",
        0x543B: "varZoneDhwTimeToStartBackup",
        0x543D: "varZoneTypeOfUsage",
        0x543E: "varZoneDhwFlowSpeed",
        0x543F: "varZoneDhwFlowSwitchSignal",
        0x5440: "varZonePrimaryTflow",
        0x5441: "varZonePrimaryTreturn",
        0x5442: "varZoneCirculationPumpRunning",
        0x5444: "varZoneOffActivityReason",
        0x5445: "varZoneVoltageMeasure",
        0x5446: "varZoneTemperatureFromVoltage",
        0x5447: "varZoneScreedDryingCurrentSetpoint",
        0x5448: "varZoneScreedDryingStartTime",
        0x5449: "varZoneScreedDryingEndTime",
        0x544A: "varZoneCtrScreedDryingRemainingTime",
        0x5450: "varZoneDhwAntilegionellaStatus",
        0x5451: "varZoneDhwAntilegionellaTimestamp",
        0x5452: "varZoneDhwStatusAntiLegionellaActive",
        0x560F: "varZoneDhwPrimaryCurrentActivities",
        0x5611: "varZoneDhwPrimaryCombiIntegrator",
        0x5612: "varZoneDhwPrimaryTFlowDhwTap",
        0x5613: "varZoneDhwPrimaryShowerTimerElapsed",
        0x561E: "varZoneDhwPrimaryAntilegionellaStatus",
        0x561F: "varZoneDhwPrimaryAntilegionellaTimestamp",
        0x5628: "varZoneDhwPrimaryTwhCurrentMode",
        0x5629: "varZoneDhwPrimaryNbShower",
        0x562A: "varZoneDhwPrimaryTankFillingLevel",
    }
)

# Exact candidates whose object semantics indicate one zone value per array
# item. The two name objects are directly paired to CP020; other entries were
# reviewed against the OBD descriptions and Core array metadata. Explicit
# ambiguous dimensions remain excluded below. Runtime uses this resolved map.
_ZONE_UNRESOLVED_CANDIDATE_INDEXES = frozenset(
    {0x340D, 0x3411, 0x5424, 0x5429, 0x542B, 0x542D}
)
_ZONE_PARENT_CANDIDATE_INDEXES = frozenset({0x540E, 0x5422, 0x5423, 0x5432})
ZONE_FAMILY_SLOT_OBJECTS: Mapping[int, frozenset[str]] = MappingProxyType(
    {0x3406: frozenset({"ehc-16", "scb-10"})}
)
ZONE_SLOT_OBJECT_SOURCES: Mapping[int, str] = MappingProxyType(
    {
        index: source_name
        for index, source_name in ZONE_OBJECT_CANDIDATE_SOURCES.items()
        if index != ZONE_FUNCTION_INDEX
        and index not in _ZONE_UNRESOLVED_CANDIDATE_INDEXES
        and index not in _ZONE_PARENT_CANDIDATE_INDEXES
    }
)

# Exact OBD Zone-named objects whose dimension is not the confirmed ten-item
# zone slot dimension (or which have no item dimension). Keep them out of
# entity construction until a source establishes their ownership dimension.
_ZONE_UNRESOLVED_EXCEPTION_SOURCES: Mapping[int, str] = {
    0x340C: "parZoneRoomUserActivitySetpoint",
    0x3412: "parZoneRoomCoolingSetpoint",
    0x5401: "varZoneProducerPowerEngineStatus",
    0x540B: "varZoneManagerHeatDemandCommandBitfield",
    0x540C: "varZoneManagerSpecialMode",
    0x540D: "varZoneHeatdemand",
    0x541A: "varZoneCtrPumpRunHours",
    0x541E: "varZoneError",
    0x5421: "varZoneHeatDemandTypeRequested",
    0x5425: "varZoneManagerProducerManagerStatusStruct",
    0x5426: "varZoneManagerSpecialActionRequestReceived",
    0x5428: "varZoneIncomingFlowTemperature",
    0x542A: "varZoneManagerSystemReturnTemperature",
    0x542C: "varZoneSystemFlowTemp",
    0x543C: "varApBSBZoneInfo",
    0x5733: "varEmZoneTReturn",
    0x340D: "parZoneFriendlyNameUserActivity",
    0x3411: "parZoneFriendlyNameCoolingSetpoint",
    0x5424: "varZoneManagerSystemFlowTemperature",
    0x5429: "varZoneManagerSystemPower",
    0x542B: "varZoneSystemPower",
    0x542D: "varZoneSystemReturnTemp",
}
ZONE_UNRESOLVED_OBJECT_SOURCES: Mapping[int, str] = MappingProxyType(
    {
        **_ZONE_UNRESOLVED_EXCEPTION_SOURCES,
        **{
            index: source_name
            for index, source_name in ZONE_OBJECT_CANDIDATE_SOURCES.items()
            if index not in ZONE_SLOT_OBJECT_SOURCES
            and index != ZONE_FUNCTION_INDEX
            and index not in _ZONE_PARENT_CANDIDATE_INDEXES
        },
    }
)

# These application-level selectors and discovery tables use their own
# scalar/record dimensions rather than the CP020 zone-slot dimension. They
# remain available to generic parent projection where their data type allows.
ZONE_PARENT_OBJECT_SOURCES: Mapping[int, str] = MappingProxyType(
    {
        0x3057: "parApPumpZoneDedicated",
        0x307E: "parApHmiQuickAccessZoneConfigurationBitField",
        0x3097: "parApZoneDiscovered",
        0x3459: "parApBSBZoneSelect",
        0x5074: "varApDiscoveredZones",
        0x5172: "varApZoneDiscovery",
        0x540E: "varZoneWinningHeatDemand",
        0x5422: "varZoneManagerConfigurationBitfieldArray",
        0x5423: "varZoneHeatDemandCommandBitfield",
        0x5432: "varZoneDiscoveryTable",
    }
)


@dataclass(frozen=True, slots=True)
class ZoneProfile:
    """One discovered CP020 item; ``function`` is None when unread."""

    node: int
    subindex: int
    function: int | None
    friendly_name: str | None = None
    node_name: str | None = None
    short_name: str | None = None

    @property
    def kind(self) -> ZoneKind:
        if self.function is None:
            return ZoneKind.UNKNOWN
        if self.function in _INACTIVE_FUNCTIONS:
            return ZoneKind.INACTIVE
        if self.function in _DHW_FUNCTIONS:
            return ZoneKind.DHW
        if self.function in _HEATING_FUNCTIONS:
            return ZoneKind.HEATING
        if zone_function_label(self.function, "en") is None:
            return ZoneKind.UNKNOWN
        return ZoneKind.OTHER

    @property
    def active(self) -> bool:
        # A numeric value absent from the manufacturer enum is not evidence
        # that this slot is a configured function. Keep it visible as unknown
        # in the profile UI, but inactive for polling/entity projection.
        return self.function is not None and self.kind not in {
            ZoneKind.INACTIVE,
            ZoneKind.UNKNOWN,
        }

    @property
    def label(self) -> str:
        """Return a display label; friendly names never affect stable IDs."""

        if self.friendly_name and self.friendly_name.strip():
            return self.friendly_name.strip()
        if self.short_name and self.short_name.strip():
            return self.short_name.strip()
        # CP020 item :01 is parameter CP020 / Zone 1; :02 is CP021 / Zone 2.
        return f"Zone {max(1, self.subindex)}"


def zone_function_label(function: int | None, language: str = "de") -> str | None:
    """Return the canonical Core/manufacturer label for a CP020 enum value."""

    if function is None or _ZONE_FUNCTION_ENUM is None:
        return None
    try:
        label = _ZONE_FUNCTION_ENUM.label(function, language)
    except (TypeError, ValueError):
        return None
    return str(label) if label else None


def _function_description(profile: ZoneProfile, language: str) -> str:
    if profile.kind is ZoneKind.INACTIVE:
        manufacturer_label = zone_function_label(profile.function, language)
        status = "Disabled" if language == "en" else "deaktiviert"
        return f"{status} ({manufacturer_label})" if manufacturer_label else status
    manufacturer_label = zone_function_label(profile.function, language)
    if profile.kind is ZoneKind.UNKNOWN:
        value = "not read" if profile.function is None else str(profile.function)
        return (
            f"Unknown function ({value})"
            if language == "en"
            else f"Unbekannte Funktion ({value})"
        )
    if profile.kind is ZoneKind.DHW:
        category = "Domestic hot water" if language == "en" else "Trinkwarmwasser"
        return f"{category} ({manufacturer_label})" if manufacturer_label else category
    if profile.kind is ZoneKind.HEATING:
        category = "Heating circuit" if language == "en" else "Heizkreis"
        return f"{category} ({manufacturer_label})" if manufacturer_label else category
    return manufacturer_label or (
        "Other function" if language == "en" else "Andere Funktion"
    )


def zone_display_name(profile: ZoneProfile, language: str = "de") -> str:
    """Build a language-consistent, node-scoped functional zone label."""

    node_name = (profile.node_name or "").strip()
    if node_name:
        node_label = f"{node_name} (Node {profile.node})"
    elif language == "en":
        node_label = f"OpenRBus node {profile.node}"
    else:
        node_label = f"OpenRBus-Knoten {profile.node}"
    name = (
        f"{node_label} — Zone {max(1, profile.subindex)} — "
        f"{_function_description(profile, language)}"
    )
    designation = (profile.friendly_name or profile.short_name or "").strip()
    if designation:
        name = f"{name} — {designation}"
    return name


def zone_subindex(register: Any, identity: Any | None = None) -> int | None:
    """Return the slot for an exact, source-mapped zone-array item."""

    if zone_association(register, identity) is not ZoneAssociation.ZONE_SLOT:
        return None
    return getattr(getattr(register, "address", None), "subindex", None)


def zone_association(register: Any, identity: Any | None = None) -> ZoneAssociation:
    """Classify from the reviewed exact object map, never labels or ranges."""

    address = getattr(register, "address", None)
    index = getattr(address, "index", None)
    subindex = getattr(address, "subindex", None)
    if not isinstance(index, int) or not isinstance(subindex, int):
        return ZoneAssociation.NOT_ZONE
    if index == ZONE_FUNCTION_INDEX:
        # CP020's item rows are parent controls, including disabled slots.
        return (
            ZoneAssociation.FUNCTION_SELECTOR
            if 1 <= subindex <= 10
            else ZoneAssociation.UNRESOLVED
        )
    if index in ZONE_UNRESOLVED_OBJECT_SOURCES:
        return ZoneAssociation.UNRESOLVED
    if index in ZONE_PARENT_OBJECT_SOURCES:
        return ZoneAssociation.PARENT
    if index in ZONE_SLOT_OBJECT_SOURCES:
        allowed_families = ZONE_FAMILY_SLOT_OBJECTS.get(index)
        if allowed_families is not None:
            resolution = getattr(identity, "registry_resolution", None)
            family = getattr(identity, "family", None) or getattr(
                resolution, "family", None
            )
            normalized_family = str(family or "").strip().casefold()
            if normalized_family not in allowed_families:
                return ZoneAssociation.UNRESOLVED
        return (
            ZoneAssociation.ZONE_SLOT
            if 1 <= subindex <= 10
            else ZoneAssociation.UNRESOLVED
        )
    return ZoneAssociation.NOT_ZONE


def zone_function_slots(rows: Any) -> tuple[int, ...]:
    """Return CP020 slots allowed by the canonical array definition.

    Family-specific RXDX profiles may only expose the first few configured
    selector rows even though the manufacturer catalog defines a longer
    CP020 array.  Once a node catalog contains that object, use its canonical
    ``max_items`` bound and let read-only discovery establish which slots are
    implemented.  Missing slots are handled as individual read failures by
    the coordinator; no family name or installation profile is assumed here.
    """

    if _ZONE_FUNCTION_DEFINITION is None:
        return ()
    if not any(
        getattr(getattr(item, "address", None), "index", None) == ZONE_FUNCTION_INDEX
        for item in rows
    ):
        return ()
    maximum = _ZONE_FUNCTION_DEFINITION.wire.max_items
    if not isinstance(maximum, int) or maximum <= 0:
        return ()
    return tuple(range(1, maximum + 1))


def profile_for(parent: Any, node: int, subindex: int) -> ZoneProfile | None:
    """Read a coordinator's discovered profile without coupling to its type."""

    profiles = getattr(parent, "zone_profiles", {}) or {}
    return profiles.get((node, subindex))


def normalized_overrides(value: object) -> dict[str, bool]:
    """Parse persisted per-zone selections, ignoring malformed legacy data."""

    if not isinstance(value, Mapping):
        return {}
    return {str(key): bool(enabled) for key, enabled in value.items()}


def normalized_selection_overrides(value: object) -> dict[str, bool | None]:
    """Normalize hierarchical visibility choices, preserving default resets."""

    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): None if enabled is None or enabled == "default" else bool(enabled)
        for key, enabled in value.items()
    }


def override_key(node: int, subindex: int) -> str:
    return f"{node}:{subindex}"


def zone_enabled(parent: Any, node: int, subindex: int) -> bool:
    """Apply selection only to a positively read, device-active CP020 slot."""

    profile = profile_for(parent, node, subindex)
    # A stale override cannot create a zone that the device says is disabled
    # (or one whose function could not be read).
    if profile is None or not profile.active:
        return False
    overrides = normalized_overrides(getattr(parent, "zone_overrides", {}))
    explicit = overrides.get(override_key(node, subindex))
    if explicit is not None:
        return explicit
    return True


def zone_is_active(parent: Any, node: int, subindex: int) -> bool:
    """Require recognized, positive CP020 evidence for child entity creation."""

    profile = profile_for(parent, node, subindex)
    return profile is not None and profile.active


def entity_zone_label(parent: Any, identity: Any, register: Any) -> str | None:
    """Return an explicit zone or heating-circuit label for entity display."""

    subindex = zone_subindex(register, identity)
    if subindex is None:
        return None
    profile = profile_for(parent, getattr(identity, "node", -1), subindex)
    language = getattr(parent, "language", "de")
    if profile and zone_enabled(parent, profile.node, subindex):
        if profile.kind is ZoneKind.HEATING:
            circuit = "Heizkreis" if language == "de" else "Heating circuit"
            friendly = (profile.friendly_name or profile.short_name or "").strip()
            return (
                f"{circuit} {max(1, subindex)} — {friendly}"
                if friendly
                else f"{circuit} {max(1, subindex)}"
            )
        return zone_display_name(profile, language)

    semantic = " ".join(
        str(getattr(register, field, "") or "")
        for field in ("internal_code", "name_en", "name_de")
    ).casefold()
    is_heating_circuit = any(
        marker in semantic
        for marker in (
            "heizkreis",
            "heating circuit",
            "hk,",
            "hk ",
            " hk",
            "hk/",
            "hk-",
        )
    )
    if is_heating_circuit:
        circuit = "Heizkreis" if language == "de" else "Heating circuit"
        return f"{circuit} {max(1, subindex)}"
    return None


def zone_device_name(profile: ZoneProfile, language: str = "de") -> str:
    """Describe a non-empty logical zone device in Home Assistant."""

    return zone_display_name(profile, language)


__all__ = [
    "ZONE_FRIENDLY_NAME_INDEX",
    "ZONE_FUNCTION_INDEX",
    "ZONE_SHORT_NAME_INDEX",
    "ZoneKind",
    "ZoneAssociation",
    "ZONE_OBJECT_CANDIDATE_SOURCES",
    "ZONE_FAMILY_SLOT_OBJECTS",
    "ZoneProfile",
    "entity_zone_label",
    "normalized_overrides",
    "normalized_selection_overrides",
    "override_key",
    "profile_for",
    "zone_device_name",
    "zone_display_name",
    "zone_enabled",
    "zone_function_label",
    "zone_association",
    "zone_is_active",
    "zone_subindex",
    "ZONE_SLOT_OBJECT_SOURCES",
    "ZONE_UNRESOLVED_OBJECT_SOURCES",
    "ZONE_PARENT_OBJECT_SOURCES",
]
