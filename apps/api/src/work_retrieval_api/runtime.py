from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time
from typing import cast
from zoneinfo import ZoneInfo

from work_retrieval_core import (
    ProductionSearchEngine,
    RetrievalPorts,
    RuntimeManifest,
    SearchEngine,
)

MANIFEST_PATH_ENV = "SEARCH_RUNTIME_MANIFEST_PATH"
PORT_FACTORY_ENV = "SEARCH_PORT_FACTORY"
DEMO_AS_OF_ENV = "SEARCH_DEMO_AS_OF"
MULTIVIEW_ENABLED_ENV = "SEARCH_ENABLE_MULTIVIEW_MAXSIM"
MULTIVIEW_ARTIFACT_ENV = "SEARCH_MULTIVIEW_ARTIFACT_KEY"
DEMO_TIMEZONE = ZoneInfo("Asia/Taipei")

RuntimeFactory = Callable[[], SearchEngine]
PortFactory = Callable[[RuntimeManifest, bool], RetrievalPorts]


def runtime_from_environment(
    environment: Mapping[str, str] | None = None,
    *,
    port_factory: PortFactory | None = None,
) -> SearchEngine:
    values = os.environ if environment is None else environment
    manifest_path = _required(values, MANIFEST_PATH_ENV)
    manifest = RuntimeManifest.from_path(manifest_path)
    enable_multiview = _boolean(values.get(MULTIVIEW_ENABLED_ENV, "false"))
    raw_multiview_artifact = values.get(MULTIVIEW_ARTIFACT_ENV)
    if enable_multiview:
        multiview_artifact = _required(values, MULTIVIEW_ARTIFACT_ENV)
    elif raw_multiview_artifact is not None:
        raise RuntimeError(f"{MULTIVIEW_ARTIFACT_ENV} requires MaxSim to be enabled")
    else:
        multiview_artifact = None
    factory = port_factory or _load_port_factory(_required(values, PORT_FACTORY_ENV))
    ports = factory(manifest, enable_multiview)
    if not isinstance(ports, RetrievalPorts):
        raise TypeError("SEARCH_PORT_FACTORY must return RetrievalPorts")

    clock: Callable[[], datetime] | None = None
    if raw_as_of := values.get(DEMO_AS_OF_ENV):
        demo_as_of = _parse_demo_as_of(raw_as_of)

        def fixed_demo_clock() -> datetime:
            return demo_as_of

        clock = fixed_demo_clock
    return ProductionSearchEngine(
        manifest,
        ports,
        enable_multiview_maxsim=enable_multiview,
        multiview_artifact_key=multiview_artifact,
        clock=clock,
    )


def _load_port_factory(spec: str) -> PortFactory:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute or "." in attribute:
        raise RuntimeError("SEARCH_PORT_FACTORY must use module:callable syntax")
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as error:
        raise RuntimeError("SEARCH_PORT_FACTORY cannot be imported") from error
    if not callable(factory):
        raise RuntimeError("SEARCH_PORT_FACTORY target must be callable")
    return cast(PortFactory, factory)


def _parse_demo_as_of(value: str) -> datetime:
    normalized = value.strip()
    if len(normalized) == 10:
        try:
            return datetime.combine(
                date.fromisoformat(normalized), time(), DEMO_TIMEZONE
            ).astimezone(UTC)
        except ValueError as error:
            raise RuntimeError("SEARCH_DEMO_AS_OF must be an ISO-8601 date or datetime") from error
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError("SEARCH_DEMO_AS_OF must be an ISO-8601 date or datetime") from error
    if parsed.tzinfo is None:
        raise RuntimeError("SEARCH_DEMO_AS_OF datetime must include a timezone")
    return parsed.astimezone(UTC)


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"missing required runtime setting: {name}")
    return value.strip()


def _boolean(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise RuntimeError(f"{MULTIVIEW_ENABLED_ENV} must be true or false")
