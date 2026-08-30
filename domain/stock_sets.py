"""Stock set domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field

from StockInvestmentTool.domain.features import now_text


SOURCE_MANUAL = "MANUAL"
SOURCE_SCREEN = "SCREEN"
SOURCE_WATCHLIST = "WATCHLIST"
SOURCE_STRATEGY_MATCH = "STRATEGY_MATCH"
SOURCE_OTHER = "OTHER"

VALID_STOCK_SET_SOURCES = {
    SOURCE_MANUAL,
    SOURCE_SCREEN,
    SOURCE_WATCHLIST,
    SOURCE_STRATEGY_MATCH,
    SOURCE_OTHER,
}


@dataclass
class StockSet:
    stock_set_id: str
    name: str
    description: str = ""
    source_type: str = SOURCE_MANUAL
    source_ref_id: str = ""
    created_at: str = field(default_factory=now_text)
    updated_at: str = field(default_factory=now_text)

    def validate(self) -> None:
        if not self.stock_set_id:
            raise ValueError("stock_set_id is required")
        if not self.name:
            raise ValueError("stock set name is required")
        if self.source_type not in VALID_STOCK_SET_SOURCES:
            raise ValueError(f"invalid stock set source_type: {self.source_type}")


@dataclass(frozen=True)
class StockSetMember:
    stock_set_id: str
    instrument: str
    added_at: str = field(default_factory=now_text)
