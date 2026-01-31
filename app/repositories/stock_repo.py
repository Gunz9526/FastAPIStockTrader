from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models.stock import StockOHLCV, StockTicker
from app.domain.schemas.stock import StockOHLCVCreate, StockTickerCreate


class StockRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_ticker(self, ticker_in: StockTickerCreate) -> StockTicker:
        db_obj = StockTicker(**ticker_in.model_dump())
        self.session.add(db_obj)
        await self.session.commit()
        await self.session.refresh(db_obj)
        return db_obj

    async def get_ticker(self, symbol: str) -> StockTicker | None:
        result = await self.session.execute(
            select(StockTicker).where(StockTicker.symbol == symbol)
        )
        return result.scalars().first()

    async def get_all_tickers(self) -> list[StockTicker]:
        result = await self.session.execute(select(StockTicker))
        return list(result.scalars().all())

    async def create_ohlcv_bulk(self, ohlcv_list: list[StockOHLCVCreate]) -> int:
        """Bulk insert OHLCV data. Returns count of inserted rows."""
        if not ohlcv_list:
            return 0

        data = [item.model_dump() for item in ohlcv_list]
        stmt = insert(StockOHLCV).values(data)
        # On conflict do nothing or update? For now just insert.
        # Ideally should handle upsert for TimescaleDB.

        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount

    async def get_ohlcv(self, symbol: str, start_date: datetime, end_date: datetime) -> list[StockOHLCV]:
        query = select(StockOHLCV).where(
            StockOHLCV.symbol == symbol,
            StockOHLCV.date_time >= start_date,
            StockOHLCV.date_time <= end_date
        ).order_by(StockOHLCV.date_time.asc())

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_active_symbols(self) -> list[str]:
        """Get list of active ticker symbols."""
        result = await self.session.execute(
            select(StockTicker.symbol).where(StockTicker.is_active == True)
        )
        return [row[0] for row in result.all()]

    async def get_ohlcv_range(self, symbol: str, start_date: datetime, end_date: datetime) -> list[StockOHLCV]:
        """Alias for get_ohlcv for consistency."""
        return await self.get_ohlcv(symbol, start_date, end_date)
