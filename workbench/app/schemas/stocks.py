from typing import Literal

from pydantic import BaseModel, Field


class StockQuery(BaseModel):
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=30, ge=1, le=200)
    passed: bool | None = None
    selected: bool | None = None
    industry: str | None = None
    search: str | None = None
    sort: Literal["rank", "total", "industry", "money_class"] = "rank"
    order: Literal["asc", "desc"] = "asc"
