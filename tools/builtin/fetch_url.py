from __future__ import annotations

import os

import logging
import time
from typing import Optional
 
import httpx
from pydantic import BaseModel, Field, field_validator

_TOOL_DESCRIPTION = "Retrieve the contents of a web page using a URL."

class FetchURLParams(BaseModel):
    url: str = Field(..., description="URL to be fetched", min_length=1, max_length=400)
    # max_urls: int = Field(5, ge=1, le=10, description="Number of results to return")

TOOL_SCHEMA = {
    "name": "fetch_url",
    "description": _TOOL_DESCRIPTION,
    "input_schema": FetchURLParams.model_json_schema()
}

def run(params: dict) -> dict:
    #make sure url is formatted correctly then fetch the contents
    pass