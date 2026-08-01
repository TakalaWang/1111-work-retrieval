from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Validated API input passed to the future production search engine."""

    text: str
    location_codes: tuple[str, ...] = ()
    duty_codes: tuple[str, ...] = ()


class SearchUnavailableError(RuntimeError):
    """The production engine cannot serve the request without violating its contract."""


@runtime_checkable
class SearchEngine(Protocol):
    """Minimal serving contract. This package intentionally provides no implementation."""

    def search(self, query: SearchQuery, *, limit: int) -> tuple[str, ...]: ...

    def close(self) -> None: ...
