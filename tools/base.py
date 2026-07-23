from dataclasses import dataclass
from typing import Callable


@dataclass
class ToolSpec:
    name: str # Tool name
    schema: dict # Tool schema
    handler: Callable[[dict], dict] # The actual function the rool runs