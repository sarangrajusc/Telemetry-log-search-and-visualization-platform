from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class LogEntry(BaseModel):
    timestamp: Optional[datetime] = None
    source: str = Field(..., description="Service or component name, e.g. 'auth-service'")
    host: Optional[str] = Field(None, description="Hostname or instance id")
    level: str = Field(..., description="INFO, WARN, ERROR, DEBUG")
    message: str
    tags: Optional[dict] = Field(default_factory=dict, description="Arbitrary key/value metadata")


class LogBatch(BaseModel):
    logs: List[LogEntry]


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
