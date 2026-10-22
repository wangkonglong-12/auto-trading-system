"""Inspectable Windows Task Scheduler XML; generation performs no registration."""

from __future__ import annotations

import hashlib
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import JsonValue

from auto_trading_system.config.node_v2 import NodeProcessConfig

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def task_plan(config: NodeProcessConfig, config_path: Path, python: Path) -> dict[str, JsonValue]:
    for path in (config_path, python):
        if (
            not path.is_absolute()
            or not path.is_file()
            or any(c in str(path) for c in ('"', "\r", "\n", "\x00"))
        ):
            raise ValueError(
                "task paths must be existing absolute files without control characters"
            )
    identity = hashlib.sha256((config.node_id + ":" + config.account_id).encode()).hexdigest()[:16]
    definitions = (
        ("Engine", ["supervise"], "08:40:00", None, None),
        ("Agent", ["agent"], "08:40:10", None, None),
        ("PullPlan", ["job", "pull-plan"], "14:30:00", "PT1M", "PT26M"),
        ("DailyExport", ["job", "daily-export"], "15:30:00", None, None),
        ("DailyRetry", ["job", "daily-export"], "15:45:00", "PT15M", "PT136M"),
        ("CatchUp", ["job", "catch-up"], "08:45:00", None, None),
    )
    tasks: list[JsonValue] = []
    for suffix, command, at, interval, duration in definitions:
        task = ET.Element("Task", {"version": "1.4", "xmlns": NS})
        registration = ET.SubElement(task, "RegistrationInfo")
        ET.SubElement(
            registration, "Description"
        ).text = f"AutoTrading V2 {identity} {suffix}; broker={config.broker}"
        triggers = ET.SubElement(task, "Triggers")
        if suffix in ("Engine", "Agent"):
            logon = ET.SubElement(triggers, "LogonTrigger")
            ET.SubElement(logon, "Enabled").text = "true"
            ET.SubElement(logon, "UserId").text = config.service_sid
        calendar = ET.SubElement(triggers, "CalendarTrigger")
        if interval:
            repeat = ET.SubElement(calendar, "Repetition")
            ET.SubElement(repeat, "Interval").text = interval
            ET.SubElement(repeat, "Duration").text = duration
            ET.SubElement(repeat, "StopAtDurationEnd").text = "false"
        ET.SubElement(calendar, "StartBoundary").text = "2026-01-01T" + at
        ET.SubElement(calendar, "Enabled").text = "true"
        week = ET.SubElement(calendar, "ScheduleByWeek")
        ET.SubElement(week, "WeeksInterval").text = "1"
        days = ET.SubElement(week, "DaysOfWeek")
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
            ET.SubElement(days, day)
        principals = ET.SubElement(task, "Principals")
        principal = ET.SubElement(principals, "Principal", {"id": "ServiceUser"})
        ET.SubElement(principal, "UserId").text = config.service_sid
        ET.SubElement(principal, "LogonType").text = "InteractiveToken"
        ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"
        settings = ET.SubElement(task, "Settings")
        for key, value in {
            "MultipleInstancesPolicy": "IgnoreNew",
            "DisallowStartIfOnBatteries": "false",
            "StopIfGoingOnBatteries": "false",
            "StartWhenAvailable": "true",
            "Enabled": "true",
            "ExecutionTimeLimit": "PT0S" if suffix in ("Engine", "Agent") else "PT10M",
        }.items():
            ET.SubElement(settings, key).text = value
        if suffix in ("Engine", "Agent"):
            restart = ET.SubElement(settings, "RestartOnFailure")
            ET.SubElement(restart, "Interval").text = "PT1M"
            ET.SubElement(restart, "Count").text = "3"
        actions = ET.SubElement(task, "Actions", {"Context": "ServiceUser"})
        execute = ET.SubElement(actions, "Exec")
        ET.SubElement(execute, "Command").text = str(python)
        ET.SubElement(execute, "Arguments").text = subprocess.list2cmdline(
            [
                "-m",
                "auto_trading_system.apps.node_process",
                "--config",
                str(config_path),
                *command,
            ]
        )
        ET.SubElement(execute, "WorkingDirectory").text = str(config.data_root)
        tasks.append(
            {
                "name": f"AutoTrading.V2.{identity}.{suffix}",
                "xml": ET.tostring(task, encoding="unicode"),
            }
        )
    return {
        "schema_major": 2,
        "broker": config.broker,
        "service_sid": config.service_sid,
        "tasks": tasks,
        "registration_performed": False,
    }
