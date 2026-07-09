from __future__ import annotations
 
import logging
import os
import time
from typing import Optional
 
import httpx
from pydantic import BaseModel, Field, field_validator
 
from duckduckgo_search import DDGS as ddgs

search_query = ""
results = ddgs.text(search_query, max_results=5)

class WebSearchParams(BaseModel):
    query: str = Field(..., description="Search query", min_length=1, max_length=400)
    num_results: int = Field(5, ge=1, le=10, description="Number of results to return")

    @field_validator("query")
    @classmethod
    def strip_query(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query cannot be empty")
        return v  # pydantic uses this return value as the final field value

TOOL_SCHEMA = {
    "name": "web_search",
    "description": (
        "Search the web for current information. Use this for facts that may "
        "have changed recently, current events, or anything you're not "
        "fully confident about."
    ),
    "input_schema": WebSearchParams.model_json_schema(),
}


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    published: Optional[str] = None # expression allows publish date to be ommitted

"""
class WebSearchError(Exception):
    
BLOCKED_DOMAINS: set[str] = set()
MAX_SNIPPET_CHARS = 300
REQUEST_TIMEOUT_S = 8.0
MAX_RETRIES = 2
CACHE_TTL_S = 300
_cache: dict[str, tuple[float, list[SearchResult]]] = {}
"""