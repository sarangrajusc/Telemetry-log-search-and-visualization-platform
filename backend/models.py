from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime

VALID_LEVELS = {"INFO", "WARN", "ERROR", "DEBUG"}


class LogEntry(BaseModel):
    timestamp: Optional[datetime] = None
    source: str = Field(..., min_length=1, max_length=200, description="Service or component name, e.g. 'auth-service'")
    host: Optional[str] = Field(None, max_length=200, description="Hostname or instance id")
    level: str = Field(..., description="INFO, WARN, ERROR, DEBUG")
    message: str = Field(..., min_length=1, max_length=10000)
    tags: Optional[dict] = Field(default_factory=dict, description="Arbitrary key/value metadata")

    @field_validator("level")
    @classmethod
    def level_must_be_known(cls, v: str) -> str:
        upper = v.upper()
        if upper not in VALID_LEVELS:
            raise ValueError(f"level must be one of {sorted(VALID_LEVELS)}, got {v!r}")
        return upper

    @field_validator("source", "message")
    @classmethod
    def no_blank_strings(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v


class LogBatch(BaseModel):
    logs: List[LogEntry] = Field(..., min_length=1, max_length=5000)


class SearchResult(BaseModel):
    id: int
    timestamp: str
    source: str
    host: Optional[str]
    level: str
    message: str
    tags: Optional[dict]


class SearchResponse(BaseModel):
    total: int
    page: int
    page_size: int
    results: List[SearchResult]
