#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Rickard Dahlstedt
"""Solinteg register translations used by the cloud-frame simulator.

The table and decoding rules are derived from Modbus Broker v5.12, whose
metadata combines the current Solinteg Home Assistant plugin with Solinteg
protocol v00.02.  This module has no third-party runtime dependencies.
"""

from __future__ import annotations

import struct
from typing import Iterator, NamedTuple, Sequence, Union


# --- ALARM, STATUS & ENUM TRANSLATION TABLES ---
# The current Home Assistant plugin is preferred where it conflicts with the
# older 2022 protocol PDF. The PDF is used to add official registers, signed
# types, status flags, safety codes, and EMS/TOU structures missing from the
# plugin-derived table.

FLAGS_OPERATION = [
    'WorkMode Abn.',
    'Emergency Stop',
    'DC Abn.',
    'Mains Abn.',
    'OffGrid Dis.',
    'Batt. Abn.',
    'Cmd Stop',
    'SocLow&NoPV',
    'ComErr Slave',
    'Meter Abn.',
    'Bypass Wait',
    'NPD Standby',
    'Generator Abn.',
    'S14 Undefined',
    'OffGrid',
    'NPD Clearing',
    'Cmd PLim',
    'OFreq PLim',
    'OTemp PLim',
    'OCurr PLim',
    'Reactive PLim',
    'Exp PLim',
    'Slow Loading',
    'OVolt PLim',
    'System PLim',
    'EMS CmdLim',
    'S27 Undefined',
    'S28 Undefined',
    'S29 Undefined',
    'S30 Undefined',
    'S31 Undefined',
    'PV PLim',
]

FLAGS_FAULT1 = [
    'Mains Lost',
    'Grid Voltage Fault',
    'Grid Frequency Fault',
    'DCI Fault',
    'ISO Over Limitation',
    'GFCI Fault',
    'PV Over Voltage',
    'Bus Voltage Fault',
    'Inverter OverTemperature',
]

FLAGS_FAULT2 = [
    '',
    'SPI Fault',
    'E2 Fault',
    'GFCI Device Fault',
    'AC Transducer Fault',
    'Relay Check Fail',
    'Internal Fan Fault',
    'External Fan Fault',
]

FLAGS_FAULT3 = [
    'Bus Hardware Fault',
    'PV Power Low',
    'Batt.Voltage Fault',
    'BAK Voltage Fault',
    'Bus Voltage Low',
    'Sys Hardware Fault',
    'BAK Over Power',
    'Inverter Over Voltage',
    'Inverter Over Freq',
    'Inverter Over Current',
    'Phase Order Err',
]

FLAGS_ARM1 = [
    'SCI Fault',
    'FLASH Fault',
    'Meter Comm Fault',
    'BMS Comm Fault',
]

FLAGS_ARM2 = [
    'BMS Comm Fault',
]

FLAGS_MPPT = [
    'MPPT1',
    'MPPT2',
    'MPPT3',
    'MPPT4',
    'MPPT5',
    'MPPT6',
    'MPPT7',
    'MPPT8',
]

FLAGS_TOU_PERIODS = [
    'Period 1 enabled',
    'Period 2 enabled',
    'Period 3 enabled',
    'Period 4 enabled',
    'Period 5 enabled',
    'Period 6 enabled',
]

BMS_ERROR_FLAGS = [
    'Internal COM Fault',
    'Voltage Sensor Fault',
    'Temperature Sensor Fault',
    'Relay Fault',
    'Cells Damage Fault',
]

BMS_PROTECTION_FLAGS = [
    'Cells Low Voltage Protection',
    'Cells High Voltage Protection',
    'Battery Module Discharge Low Voltage Protection',
    'Battery Module Charge Over Voltage Protection',
    'Charge Low Temperature Protection',
    'Charge High Temperature Protection',
    'Discharge Low Temperature Protection',
    'Discharge High Temperature Protection',
    'Battery Module Charge Over-current Protection',
    'Battery Module Discharge Over-current Protection',
    'Battery Module Low Voltage Protection',
    'Battery Module Over Voltage Protection',
    'Power Terminal Over Temperature Protection',
    'Ambient Low Temperature Protection',
    'Ambient High Temperature Protection',
    'Leakage Current Protection',
]

BMS_WARNING_FLAGS = [
    'Cells Low Voltage Warning',
    'Cells High Voltage Warning',
    'Battery Module Discharge Low Voltage Warning',
    'Battery Module Charge Over Voltage Warning',
    'Charge Low Temperature Warning',
    'Charge High Temperature Warning',
    'Discharge Low Temperature Warning',
    'Discharge High Temperature Warning',
    'Battery Module Charge Over-current Warning',
    'Battery Module Discharge Over-current Warning',
    'Battery Module Low Voltage Warning',
    'Battery Module High Voltage Warning',
    'Power Terminal Over Temperature Warning',
    'Ambient Low Temperature Warning',
    'Ambient High Temperature Warning',
]

SAFETY_CODES = {
    0: 'Reserved / disabled',
    1: 'Reserved / disabled',
    2: 'Reserved / disabled',
    3: 'Reserved / disabled',
    4: 'Customized code 1',
    6: 'Customized code 2',
    10: '50Hz Default',
    11: '60Hz Default',
    12: 'VDE4105',
    13: 'AS4777.2(A)',
    14: 'AS4777.2(NZ)',
    16: 'EN50549',
    18: 'IEC61727 (50Hz)',
    19: 'IEC61727 (60Hz)',
    24: 'Italy',
    25: 'Czech (A1)',
    26: 'Czech (A2)',
    29: 'EN50549 (PL)',
    31: 'Belgium C10/11',
    35: 'VDE0126',
    36: 'Italy (MV) CEI 0-16',
    37: 'South Africa NRS 097-2-1',
    40: 'G98',
    41: 'G99',
    42: 'Austria TOR Erzeuger',
    46: 'AS4777.2(B)',
    47: 'ES:UNE217002',
    48: 'AS4777.2(C)',
    49: 'ES:NTS631',
}

BATTERY_BRANDS = {
    1: 'Solinteg',
    2: 'EMS',
    10: 'Wattsonic Li-HV',
    11: 'AOBOET',
    12: 'DYNESS',
    13: 'Pylon',
    14: 'Soluna',
    15: 'SheenPlus',
    16: 'WECO',
}

INVERTER_MODELS = {
    7680: 'MHT-4K-25',
    7681: 'MHT-5K-25',
    7682: 'MHT-6K-25',
    7683: 'MHT-8K-25',
    7684: 'MHT-10K-25',
    7685: 'MHT-12K-25',
    7686: 'MHT-10K-40',
    7687: 'MHT-12K-40',
    7688: 'MHT-15K-40',
    7689: 'MHT-20K-40',
    7936: 'MHS-3K-30D',
    7937: 'MHS-3.6K-30D',
    7938: 'MHS-4.2K-30D',
    7939: 'MHS-4.6K-30D',
    7940: 'MHS-5K-30D',
    7941: 'MHS-6K-30D',
    7942: 'MHS-7K-30D',
    7943: 'MHS-8K-30D',
    7944: 'MHS-3K-30S',
    7945: 'MHS-3.6K-30S',
    8192: 'MHT-25K-100',
    8193: 'MHT-30K-100',
    8194: 'MHT-36K-100',
    8195: 'MHT-40K-100',
    8196: 'MHT-50K-100',
    10240: 'M2HT-3K-30',
    10241: 'M2HT-3.6K-30',
    10242: 'M2HT-4.2K-30',
    10243: 'M2HT-4.6K-30',
    10244: 'M2HT-5K-30',
    10245: 'M2HT-6K-30',
    10496: 'M2HT-25K-150',
    10497: 'M2HT-29.9K-150',
    10498: 'M2HT-30K-150',
    10499: 'M2HT-40K-150',
    10500: 'M2HT-50K-150',
    10752: 'MRT-25K-100',
    10753: 'MRT-30K-100',
    10754: 'MRT-36K-100',
    10755: 'MRT-40K-100',
    10756: 'MRT-50K-100',
    11008: 'M2HT-75K-300',
    11009: 'M2HT-80K-300',
    11010: 'M2HT-99K-300',
    11011: 'M2HT-100K-300',
    11012: 'M2HT-110K-300',
    11013: 'M2HT-125K-300',
}


# --- REGISTER METADATA MAP (0-BASED INNER LOGIC) ---
# Synced against plugin_solinteg(1).py and Solinteg protocol v00.02.
REGISTER_METADATA = {
    10000: {'name': 'Device Serial Number', 'type': 'STR', 'words': 8, 'scale': 1, 'unit': ''},
    10008: {'name': 'Inverter Model Code', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': INVERTER_MODELS},
    10011: {'name': 'Firmware Version', 'type': 'WORDS', 'words': 4, 'min_words': 2, 'scale': 1, 'unit': ''},
    10100: {'name': 'Inverter RTC Year/Month', 'type': 'PACKED_YM', 'words': 1, 'scale': 1, 'unit': ''},
    10101: {'name': 'Inverter RTC Day/Hour', 'type': 'PACKED_DH', 'words': 1, 'scale': 1, 'unit': ''},
    10102: {'name': 'Inverter RTC Minute/Second', 'type': 'PACKED_MS', 'words': 1, 'scale': 1, 'unit': ''},
    10104: {'name': 'Active Safety Code', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': SAFETY_CODES},
    10105: {'name': 'Inverter Working Status', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Waiting', 1: 'Self checking', 2: 'On Grid generating', 3: 'Fault', 4: 'Firmware upgrade', 5: 'Off Grid generating'}},
    10110: {'name': 'Inverter Operation Flags', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': '', 'flags': FLAGS_OPERATION},
    10112: {'name': 'Fault Flags 1', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': '', 'flags': FLAGS_FAULT1},
    10114: {'name': 'Fault Flags 2', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': '', 'flags': FLAGS_FAULT2},
    10120: {'name': 'Fault Flags 3', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': '', 'flags': FLAGS_FAULT3},
    10994: {'name': 'Meter Active Power L1', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    10996: {'name': 'Meter Active Power L2', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    10998: {'name': 'Meter Active Power L3', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    11000: {'name': 'Meter Active Power Total', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    11002: {'name': 'Meter Grid Export Total', 'type': 'U32', 'words': 2, 'scale': 0.01, 'unit': 'kWh'},
    11004: {'name': 'Meter Grid Import Total', 'type': 'U32', 'words': 2, 'scale': 0.01, 'unit': 'kWh'},
    11006: {'name': 'Grid AB Line Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    11007: {'name': 'Grid BC Line Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    11008: {'name': 'Grid CA Line Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    11009: {'name': 'Grid Phase A Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    11010: {'name': 'Grid Phase A Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    11011: {'name': 'Grid Phase B Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    11012: {'name': 'Grid Phase B Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    11013: {'name': 'Grid Phase C Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    11014: {'name': 'Grid Phase C Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    11015: {'name': 'Grid System Frequency', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': 'Hz'},
    11016: {'name': 'AC Active Power (Inverter Load)', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    11018: {'name': 'Energy AC Generation Today', 'type': 'U32', 'words': 2, 'scale': 0.1, 'unit': 'kWh'},
    11020: {'name': 'Energy AC Generation Total', 'type': 'U32', 'words': 2, 'scale': 0.1, 'unit': 'kWh'},
    11022: {'name': 'Total Generation Hours', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': 'h'},
    11028: {'name': 'PV Input Power Total', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': 'W'},
    11032: {'name': 'Inv Temperature Phase-R', 'type': 'S16', 'words': 1, 'scale': 0.1, 'unit': '°C'},
    11033: {'name': 'Inv Temperature Phase-S', 'type': 'S16', 'words': 1, 'scale': 0.1, 'unit': '°C'},
    11034: {'name': 'Inv Temperature Phase-T', 'type': 'S16', 'words': 1, 'scale': 0.1, 'unit': '°C'},
    11035: {'name': 'Inv Temperature Radiator', 'type': 'S16', 'words': 1, 'scale': 0.1, 'unit': '°C'},
    11038: {'name': 'PV1 Input Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    11039: {'name': 'PV1 Input Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    11040: {'name': 'PV2 Input Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    11041: {'name': 'PV2 Input Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    11042: {'name': 'PV3 Input Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    11043: {'name': 'PV3 Input Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    11044: {'name': 'PV4 Input Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    11045: {'name': 'PV4 Input Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    11046: {'name': 'PV5 Input Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    11047: {'name': 'PV5 Input Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    11048: {'name': 'PV6 Input Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    11049: {'name': 'PV6 Input Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    11062: {'name': 'PV1 Input Power', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': 'W'},
    11064: {'name': 'PV2 Input Power', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': 'W'},
    11066: {'name': 'PV3 Input Power', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': 'W'},
    11068: {'name': 'PV4 Input Power', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': 'W'},
    11070: {'name': 'PV5 Input Power', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': 'W'},
    11072: {'name': 'PV6 Input Power', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': 'W'},
    18000: {'name': 'Fault ARM Flags 1', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': '', 'flags': FLAGS_ARM1},
    18004: {'name': 'Fault ARM Flags 2', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': '', 'flags': FLAGS_ARM2},
    20000: {'name': 'Inverter RTC Year/Month Setting', 'type': 'PACKED_YM', 'words': 1, 'scale': 1, 'unit': ''},
    20001: {'name': 'Inverter RTC Day/Hour Setting', 'type': 'PACKED_DH', 'words': 1, 'scale': 1, 'unit': ''},
    20002: {'name': 'Inverter RTC Minute/Second Setting', 'type': 'PACKED_MS', 'words': 1, 'scale': 1, 'unit': ''},
    25000: {'name': 'Safety Code Setting', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': SAFETY_CODES},
    25008: {'name': 'Inverter Command', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {257: 'Start', 256: 'Stop Soft', 1028: 'Stop Full'}},
    25009: {'name': 'Inverter Restart', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {1: 'Restart'}},
    25015: {'name': 'Overload Method Setting', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Rated', 1: '110% Overloading', 2: 'Limit'}},
    25020: {'name': 'Shadow Scan', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Off', 32767: 'On'}, 'flags': FLAGS_MPPT},
    25100: {'name': 'Export Limit Switch', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Off', 1: 'On'}},
    25103: {'name': 'Export Limit Value', 'type': 'S16', 'words': 1, 'scale': 0.1, 'unit': '%'},
    25104: {'name': 'Smart Meter Communication Status', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Meter abnormal', 1: 'Meter normal'}},
    25105: {'name': 'EMS Meter Active Power Phase A', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    25107: {'name': 'EMS Meter Active Power Phase B', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    25109: {'name': 'EMS Meter Active Power Phase C', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    25118: {'name': 'Reactive Power Limit Percentage', 'type': 'S16', 'words': 1, 'scale': 0.1, 'unit': '%'},
    25120: {'name': 'Power Factor Setting', 'type': 'S16', 'words': 1, 'scale': 0.001, 'unit': ''},
    25121: {'name': 'Reactive Power Control Mode', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Off', 1: 'Power factor', 2: 'Qt', 3: 'Q(P)', 4: 'Q(U)'}},
    28000: {'name': 'Fault Recovery Voltage Lower Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    28001: {'name': 'Fault Recovery Voltage Upper Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    28002: {'name': 'Fault Recovery Frequency Lower Limit', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': 'Hz'},
    28003: {'name': 'Fault Recovery Frequency Upper Limit', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': 'Hz'},
    28004: {'name': 'Level-1 Undervoltage Protection Threshold', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    28005: {'name': 'Level-1 Undervoltage Protection Duration', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': 'periods'},
    28006: {'name': 'Level-1 Overvoltage Protection Threshold', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    28007: {'name': 'Level-1 Overvoltage Protection Duration', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': 'periods'},
    28012: {'name': 'Level-1 Underfrequency Protection Threshold', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': 'Hz'},
    28013: {'name': 'Level-1 Underfrequency Protection Duration', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': 'periods'},
    28014: {'name': 'Level-1 Overfrequency Protection Threshold', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': 'Hz'},
    28015: {'name': 'Level-1 Overfrequency Protection Duration', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': 'periods'},
    30200: {'name': 'Backup Phase A Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    30201: {'name': 'Backup Phase A Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    30202: {'name': 'Backup Phase A Frequency', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': 'Hz'},
    30204: {'name': 'Backup Phase A Active Power', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    30210: {'name': 'Backup Phase B Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    30211: {'name': 'Backup Phase B Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    30212: {'name': 'Backup Phase B Frequency', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': 'Hz'},
    30214: {'name': 'Backup Phase B Active Power', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    30220: {'name': 'Backup Phase C Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    30221: {'name': 'Backup Phase C Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    30222: {'name': 'Backup Phase C Frequency', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': 'Hz'},
    30224: {'name': 'Backup Phase C Active Power', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    30230: {'name': 'Total Backup Active Power', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    30236: {'name': 'Inverter Phase A Active Power', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    30242: {'name': 'Inverter Phase B Active Power', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    30248: {'name': 'Inverter Phase C Active Power', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    30254: {'name': 'Battery Pack Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    30255: {'name': 'Battery Pack Current', 'type': 'S16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    30256: {'name': 'Battery Charge Direction', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Discharging', 1: 'Charging'}},
    30258: {'name': 'Battery Pack Power', 'type': 'S32', 'words': 2, 'scale': 1, 'unit': 'W'},
    31000: {'name': 'Daily Energy Injected to Grid', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'kWh'},
    31001: {'name': 'Daily Purchased Energy', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'kWh'},
    31002: {'name': 'Daily Energy Output on Backup Port', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'kWh'},
    31003: {'name': 'Daily Battery Charging Energy', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'kWh'},
    31004: {'name': 'Daily Battery Discharging Energy', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'kWh'},
    31005: {'name': 'Daily PV Generation', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'kWh'},
    31006: {'name': 'Daily Load Consumption', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'kWh'},
    31008: {'name': 'Daily Energy Purchased from Grid at Inverter Side', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'kWh'},
    31102: {'name': 'Grid Export Total', 'type': 'U32', 'words': 2, 'scale': 0.1, 'unit': 'kWh'},
    31104: {'name': 'Grid Import Total', 'type': 'U32', 'words': 2, 'scale': 0.1, 'unit': 'kWh'},
    31106: {'name': 'Backup Output Energy Total', 'type': 'U32', 'words': 2, 'scale': 0.1, 'unit': 'kWh'},
    31108: {'name': 'Battery Charge Total', 'type': 'U32', 'words': 2, 'scale': 0.1, 'unit': 'kWh'},
    31110: {'name': 'Battery Discharge Total', 'type': 'U32', 'words': 2, 'scale': 0.1, 'unit': 'kWh'},
    31112: {'name': 'PV Generation Total', 'type': 'U32', 'words': 2, 'scale': 0.1, 'unit': 'kWh'},
    31114: {'name': 'House Energy Total', 'type': 'U32', 'words': 2, 'scale': 0.1, 'unit': 'kWh'},
    31118: {'name': 'Grid Import at Inverter Side Total', 'type': 'U32', 'words': 2, 'scale': 0.1, 'unit': 'kWh'},
    32000: {'name': 'Battery Type Code', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': ''},
    32001: {'name': 'Battery Strings Count', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': ''},
    32002: {'name': 'Battery Protocol Version', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': ''},
    32003: {'name': 'Battery Firmware', 'type': 'BYTE_VERSION', 'words': 1, 'scale': 1, 'unit': ''},
    32004: {'name': 'Battery Hardware', 'type': 'BYTE_VERSION', 'words': 1, 'scale': 1, 'unit': ''},
    32005: {'name': 'BMS Charge Current Maximum (Legacy)', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    32006: {'name': 'BMS Discharge Current Maximum (Legacy)', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    32007: {'name': 'Battery Rated Capacity', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': 'Wh'},
    32020: {'name': 'Battery Manufacturer', 'type': 'STR', 'words': 8, 'scale': 1, 'unit': ''},
    33000: {'name': 'Battery State of Charge (SOC)', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': '%'},
    33001: {'name': 'Battery State of Health (SOH)', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': '%'},
    33002: {'name': 'BMS Operational Status', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Initialization / Standby', 1: 'Normal Status', 3: 'Charging Activated', 2: 'Discharging Activated', 4: 'Fault State', 5: 'Flash / Upgrading Mode'}},
    33003: {'name': 'Battery Core Temperature', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': '°C'},
    33008: {'name': 'BMS Max Cell Temperature ID', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': ''},
    33009: {'name': 'BMS Max Cell Temperature', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': '°C'},
    33010: {'name': 'BMS Min Cell Temperature ID', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': ''},
    33011: {'name': 'BMS Min Cell Temperature', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': '°C'},
    33012: {'name': 'BMS Max Cell Voltage ID', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': ''},
    33013: {'name': 'BMS Max Cell Voltage', 'type': 'U16', 'words': 1, 'scale': 0.001, 'unit': 'V'},
    33014: {'name': 'BMS Min Cell Voltage ID', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': ''},
    33015: {'name': 'BMS Min Cell Voltage', 'type': 'U16', 'words': 1, 'scale': 0.001, 'unit': 'V'},
    33016: {'name': 'BMS Error Code', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': '', 'flags': BMS_ERROR_FLAGS},
    33018: {'name': 'BMS Warning Code', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': '', 'flags': BMS_WARNING_FLAGS},
    33021: {'name': 'Battery Charge Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    33023: {'name': 'Battery Discharge Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    50000: {'name': 'Working Mode Selection', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {257: 'General', 258: 'Economic', 259: 'UPS', 260: 'PeakShift', 261: 'Feed-In', 512: 'Off-Grid', 769: 'EMS ACCtrl', 770: 'EMS General', 771: 'EMS BattCtrl', 772: 'EMS Off-Grid', 1024: 'ToU'}},
    50001: {'name': 'UPS Function Switch', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Off', 1: 'On'}},
    50004: {'name': 'Off-grid Voltage Setting', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    50005: {'name': 'Off-grid Frequency Setting', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': 'Hz'},
    50006: {'name': 'Grid Unbalanced Output Switch', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Off', 1: 'On'}},
    50007: {'name': 'Import Limit Switch', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Off', 1: 'On'}},
    50009: {'name': 'Import Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'kW'},
    50010: {'name': 'Parallel Master-Slave Sign', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Independent Operating', 1: 'Parallel (Slave)', 2: 'Parallel (Master)'}},
    50012: {'name': 'Battery Protection Relax', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Off', 1: 'On'}},
    50200: {'name': 'Grid Mode Toggle', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'On-Grid', 1: 'Off-Grid'}},
    50201: {'name': 'Clear Off-grid Overload Protection', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {1: 'Clear protection flag'}},
    50202: {'name': 'EMS AC Ctrl Scheduling Mode', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Off', 1: 'Total Power', 2: 'Phase Level Power'}},
    50203: {'name': 'EMS AC Ctrl Total AC Power Target', 'type': 'S16', 'words': 1, 'scale': 0.01, 'unit': 'kW'},
    50204: {'name': 'EMS AC Ctrl Phase A Power Target', 'type': 'S16', 'words': 1, 'scale': 0.01, 'unit': 'kW'},
    50205: {'name': 'EMS AC Ctrl Phase B Power Target', 'type': 'S16', 'words': 1, 'scale': 0.01, 'unit': 'kW'},
    50206: {'name': 'EMS AC Ctrl Phase C Power Target', 'type': 'S16', 'words': 1, 'scale': 0.01, 'unit': 'kW'},
    50207: {'name': 'EMS BattCtrl Charge/Discharge Target', 'type': 'S16', 'words': 1, 'scale': 0.01, 'unit': 'kW'},
    50208: {'name': 'EMS BattCtrl Max Grid Export', 'type': 'S16', 'words': 1, 'scale': 0.01, 'unit': 'kW'},
    50209: {'name': 'EMS BattCtrl Max Grid Import', 'type': 'S16', 'words': 1, 'scale': 0.01, 'unit': 'kW'},
    50210: {'name': 'EMS BattCtrl Priority of Power Output', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'PV', 1: 'Battery'}},
    50211: {'name': 'EMS Off-grid PV Power Target', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': 'kW'},
    52500: {'name': 'Battery Brand Configuration', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': BATTERY_BRANDS},
    52501: {'name': 'Battery Protocol Configuration', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Default / N/A', 1: 'Default / N/A', 2: 'EBS-5150'}},
    52502: {'name': 'Battery SOC Protection On Grid Switch', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Off', 1: 'On'}},
    52503: {'name': 'Battery SOC Min On Grid Setting', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': '%'},
    52504: {'name': 'Battery SOC Protection Off Grid Switch', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'Off', 1: 'On'}},
    52505: {'name': 'Battery SOC Min Off Grid Setting', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': '%'},
    52601: {'name': 'Battery Charge Current Limit Setting', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    52603: {'name': 'Battery Discharge Current Limit Setting', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    53006: {'name': 'TOU Period Enable Flags', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'flags': FLAGS_TOU_PERIODS},
    53007: {'name': 'TOU Period 1 Charge/Discharge Mode', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'None', 1: 'Charge', 2: 'Discharge'}},
    53008: {'name': 'TOU Period 1 Battery Charge Source', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'PV', 1: 'PV + Grid'}},
    53009: {'name': 'TOU Period 1 Reserved 1', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {255: 'Reserved marker'}},
    53010: {'name': 'TOU Period 1 Power Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': '%'},
    53011: {'name': 'TOU Period 1 Reserved 2', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {255: 'Reserved marker'}},
    53012: {'name': 'TOU Period 1 Start Time', 'type': 'HHMM', 'words': 1, 'scale': 1, 'unit': ''},
    53013: {'name': 'TOU Period 1 Stop Time', 'type': 'HHMM', 'words': 1, 'scale': 1, 'unit': ''},
    53014: {'name': 'TOU Period 2 Charge/Discharge Mode', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'None', 1: 'Charge', 2: 'Discharge'}},
    53015: {'name': 'TOU Period 2 Battery Charge Source', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'PV', 1: 'PV + Grid'}},
    53016: {'name': 'TOU Period 2 Reserved 1', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {255: 'Reserved marker'}},
    53017: {'name': 'TOU Period 2 Power Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': '%'},
    53018: {'name': 'TOU Period 2 Reserved 2', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {255: 'Reserved marker'}},
    53019: {'name': 'TOU Period 2 Start Time', 'type': 'HHMM', 'words': 1, 'scale': 1, 'unit': ''},
    53020: {'name': 'TOU Period 2 Stop Time', 'type': 'HHMM', 'words': 1, 'scale': 1, 'unit': ''},
    53021: {'name': 'TOU Period 3 Charge/Discharge Mode', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'None', 1: 'Charge', 2: 'Discharge'}},
    53022: {'name': 'TOU Period 3 Battery Charge Source', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'PV', 1: 'PV + Grid'}},
    53023: {'name': 'TOU Period 3 Reserved 1', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {255: 'Reserved marker'}},
    53024: {'name': 'TOU Period 3 Power Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': '%'},
    53025: {'name': 'TOU Period 3 Reserved 2', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {255: 'Reserved marker'}},
    53026: {'name': 'TOU Period 3 Start Time', 'type': 'HHMM', 'words': 1, 'scale': 1, 'unit': ''},
    53027: {'name': 'TOU Period 3 Stop Time', 'type': 'HHMM', 'words': 1, 'scale': 1, 'unit': ''},
    53028: {'name': 'TOU Period 4 Charge/Discharge Mode', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'None', 1: 'Charge', 2: 'Discharge'}},
    53029: {'name': 'TOU Period 4 Battery Charge Source', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'PV', 1: 'PV + Grid'}},
    53030: {'name': 'TOU Period 4 Reserved 1', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {255: 'Reserved marker'}},
    53031: {'name': 'TOU Period 4 Power Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': '%'},
    53032: {'name': 'TOU Period 4 Reserved 2', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {255: 'Reserved marker'}},
    53033: {'name': 'TOU Period 4 Start Time', 'type': 'HHMM', 'words': 1, 'scale': 1, 'unit': ''},
    53034: {'name': 'TOU Period 4 Stop Time', 'type': 'HHMM', 'words': 1, 'scale': 1, 'unit': ''},
    53035: {'name': 'TOU Period 5 Charge/Discharge Mode', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'None', 1: 'Charge', 2: 'Discharge'}},
    53036: {'name': 'TOU Period 5 Battery Charge Source', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'PV', 1: 'PV + Grid'}},
    53037: {'name': 'TOU Period 5 Reserved 1', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {255: 'Reserved marker'}},
    53038: {'name': 'TOU Period 5 Power Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': '%'},
    53039: {'name': 'TOU Period 5 Reserved 2', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {255: 'Reserved marker'}},
    53040: {'name': 'TOU Period 5 Start Time', 'type': 'HHMM', 'words': 1, 'scale': 1, 'unit': ''},
    53041: {'name': 'TOU Period 5 Stop Time', 'type': 'HHMM', 'words': 1, 'scale': 1, 'unit': ''},
    53042: {'name': 'TOU Period 6 Charge/Discharge Mode', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'None', 1: 'Charge', 2: 'Discharge'}},
    53043: {'name': 'TOU Period 6 Battery Charge Source', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {0: 'PV', 1: 'PV + Grid'}},
    53044: {'name': 'TOU Period 6 Reserved 1', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {255: 'Reserved marker'}},
    53045: {'name': 'TOU Period 6 Power Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': '%'},
    53046: {'name': 'TOU Period 6 Reserved 2', 'type': 'U16', 'words': 1, 'scale': 1, 'unit': '', 'map': {255: 'Reserved marker'}},
    53047: {'name': 'TOU Period 6 Start Time', 'type': 'HHMM', 'words': 1, 'scale': 1, 'unit': ''},
    53048: {'name': 'TOU Period 6 Stop Time', 'type': 'HHMM', 'words': 1, 'scale': 1, 'unit': ''},
    53500: {'name': 'EMS BMS Version', 'type': 'STR', 'words': 8, 'scale': 1, 'unit': ''},
    53508: {'name': 'EMS BMS Status', 'type': 'BMS_STATUS', 'words': 1, 'scale': 1, 'unit': ''},
    53509: {'name': 'EMS BMS Error Code', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': '', 'flags': BMS_ERROR_FLAGS},
    53511: {'name': 'EMS BMS Protection Code', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': '', 'flags': BMS_PROTECTION_FLAGS},
    53513: {'name': 'EMS BMS Warning Code', 'type': 'U32', 'words': 2, 'scale': 1, 'unit': '', 'flags': BMS_WARNING_FLAGS},
    53515: {'name': 'EMS BMS Charge Voltage Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    53516: {'name': 'EMS BMS Maximum Charge Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    53517: {'name': 'EMS BMS Discharge Voltage Limit', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    53518: {'name': 'EMS BMS Maximum Discharge Current', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    53519: {'name': 'EMS BMS Battery SOC', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': '%'},
    53520: {'name': 'EMS BMS Battery SOH', 'type': 'U16', 'words': 1, 'scale': 0.01, 'unit': '%'},
    53521: {'name': 'EMS BMS Battery Voltage', 'type': 'U16', 'words': 1, 'scale': 0.1, 'unit': 'V'},
    53522: {'name': 'EMS BMS Battery Current', 'type': 'S16', 'words': 1, 'scale': 0.1, 'unit': 'A'},
    53523: {'name': 'EMS BMS Battery Temperature', 'type': 'S16', 'words': 1, 'scale': 0.1, 'unit': '°C'},
}


class DecodedRegister(NamedTuple):
    """One semantic register value, or one still-unmapped raw register word."""

    address: int
    name: str
    raw_words: tuple[int, ...]
    value: str


def _format_scaled(raw_value: int, scale: float) -> Union[int, float]:
    calculated = raw_value * scale
    if scale >= 1:
        return int(round(calculated))
    precision = len(str(scale).split(".")[1])
    return round(calculated, precision)


def _format_flags(raw_value: int, flags: Sequence[str], bit_count: int) -> str:
    active = [
        flags[bit]
        for bit in range(min(bit_count, len(flags)))
        if raw_value & (1 << bit) and flags[bit]
    ]
    known_mask = sum(
        1 << bit
        for bit in range(min(bit_count, len(flags)))
        if flags[bit]
    )
    unknown_mask = (1 << bit_count) - 1
    unknown = raw_value & ~known_mask & unknown_mask
    if unknown:
        active.append(f"Unknown bits 0x{unknown:0{bit_count // 4}X}")
    return " | ".join(active)


def _decode_known(meta: dict[str, object], words: Sequence[int]) -> str:
    register_type = str(meta["type"])
    scale = float(meta["scale"])
    unit = str(meta["unit"])

    if register_type == "U16":
        raw = words[0]
        value = _format_scaled(raw, scale)
        decoded = f"{value} {unit}" if unit else str(value)
        value_map = meta.get("map")
        if isinstance(value_map, dict) and raw in value_map:
            decoded += f" ({value_map[raw]})"
        elif "flags" in meta:
            flags = _format_flags(raw, meta["flags"], 16)
            decoded += f" ({flags})" if flags else " (Off / none)"
        return decoded

    if register_type == "S16":
        raw = struct.unpack(">h", struct.pack(">H", words[0]))[0]
        value = _format_scaled(raw, scale)
        return f"{value} {unit}" if unit else str(value)

    if register_type in ("U32", "S32"):
        combined = (words[0] << 16) | words[1]
        if register_type == "S32":
            raw = struct.unpack(">i", struct.pack(">I", combined))[0]
        else:
            raw = combined
        value = _format_scaled(raw, scale)
        decoded = f"{value} {unit}" if unit else str(value)
        if register_type == "U32" and "flags" in meta:
            flags = _format_flags(raw, meta["flags"], 32)
            decoded += f" ({flags})" if flags else " (OK)"
        return decoded

    if register_type == "STR":
        raw_bytes = b"".join(word.to_bytes(2, "big") for word in words)
        return raw_bytes.decode("ascii", errors="ignore").rstrip("\x00 ")

    if register_type == "WORDS":
        raw_bytes = b"".join(word.to_bytes(2, "big") for word in words)
        if len(raw_bytes) >= 8:
            return (
                f"V{'.'.join(str(byte) for byte in raw_bytes[0:4])}-"
                f"{'.'.join(str(byte) for byte in raw_bytes[4:8])}"
            )
        return f"V{'.'.join(str(byte) for byte in raw_bytes)}"

    raw = words[0]
    if register_type == "BYTE_VERSION":
        return f"{(raw >> 8) & 0xFF}.{raw & 0xFF}"
    if register_type == "PACKED_YM":
        return f"20{(raw >> 8) & 0xFF:02d}-{raw & 0xFF:02d}"
    if register_type == "PACKED_DH":
        return f"day {(raw >> 8) & 0xFF:02d}, hour {raw & 0xFF:02d}"
    if register_type == "PACKED_MS":
        return f"{(raw >> 8) & 0xFF:02d}:{raw & 0xFF:02d}"
    if register_type == "HHMM":
        return f"{(raw >> 8) & 0xFF:02d}:{raw & 0xFF:02d}"
    if register_type == "BMS_STATUS":
        running_states = {
            0: "Sleep",
            1: "Charge",
            2: "Discharge",
            3: "Standby",
            4: "Fault",
        }
        state_code = raw & 0xFF
        state = running_states.get(state_code, f"Unknown state {state_code}")
        commands = []
        if raw & (1 << 8):
            commands.append("On-grid discharge enabled")
        if raw & (1 << 9):
            commands.append("Off-grid discharge enabled")
        if raw & (1 << 10):
            commands.append("Charge enabled")
        if raw & (1 << 11):
            commands.append("Force charge")
        if commands:
            return f"{state} ({' | '.join(commands)})"
        return state

    return f"Unsupported translation type {register_type}"


def decode_register_range(
    start_address: int,
    values: Sequence[int],
) -> Iterator[DecodedRegister]:
    """Decode one contiguous register range with the Broker v5.12 table."""

    index = 0
    while index < len(values):
        address = start_address + index
        meta = REGISTER_METADATA.get(address)
        if meta is not None:
            word_count = int(meta["words"])
            minimum = int(meta.get("min_words", word_count))
            remaining = len(values) - index
            if remaining >= minimum:
                consume = min(word_count, remaining)
                raw_words = tuple(values[index:index + consume])
                try:
                    decoded = _decode_known(meta, raw_words)
                except (IndexError, KeyError, TypeError, ValueError, struct.error) as error:
                    decoded = f"Decode error: {type(error).__name__}: {error}"
                yield DecodedRegister(
                    address,
                    str(meta["name"]),
                    raw_words,
                    decoded,
                )
                index += consume
                continue

        raw = int(values[index])
        yield DecodedRegister(address, "Raw Field", (raw,), str(raw))
        index += 1
