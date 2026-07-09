from __future__ import annotations
 
import logging
import os
import time
from typing import Optional
 
import httpx
from pydantic import BaseModel, Field, field_validator


class FetchURLParams(BaseModel):
    url: str = Field(..., description="URL to be fetched", min_length=1, max_length=400)
    # max_urls: int = Field(5, ge=1, le=10, description="Number of results to return")