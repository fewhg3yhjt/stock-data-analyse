"""Stock set service."""

from __future__ import annotations

from uuid import uuid4

from StockInvestmentTool.domain.stock_sets import SOURCE_MANUAL, StockSet
from StockInvestmentTool.repositories.backend_domain import BackendDomainRepository


class StockSetService:
    def __init__(self, repository: BackendDomainRepository | None = None):
        self.repository = repository or BackendDomainRepository()

    def create_stock_set(
        self,
        name: str,
        instruments: list[str] | None = None,
        *,
        description: str = "",
        source_type: str = SOURCE_MANUAL,
        source_ref_id: str = "",
        stock_set_id: str | None = None,
    ) -> StockSet:
        stock_set = StockSet(
            stock_set_id=stock_set_id or f"ss_{uuid4().hex}",
            name=name,
            description=description,
            source_type=source_type,
            source_ref_id=source_ref_id,
        )
        self.repository.create_stock_set(stock_set)
        if instruments:
            self.repository.add_stock_set_members(stock_set.stock_set_id, instruments)
        return stock_set

    def add_members(self, stock_set_id: str, instruments: list[str]) -> int:
        if not self.repository.get_stock_set(stock_set_id):
            raise KeyError(f"unknown stock set: {stock_set_id}")
        return self.repository.add_stock_set_members(stock_set_id, instruments)

    def remove_members(self, stock_set_id: str, instruments: list[str]) -> int:
        if not self.repository.get_stock_set(stock_set_id):
            raise KeyError(f"unknown stock set: {stock_set_id}")
        return self.repository.remove_stock_set_members(stock_set_id, instruments)

    def snapshot_members(self, stock_set_id: str) -> list[str]:
        if not self.repository.get_stock_set(stock_set_id):
            raise KeyError(f"unknown stock set: {stock_set_id}")
        return self.repository.list_stock_set_members(stock_set_id)
