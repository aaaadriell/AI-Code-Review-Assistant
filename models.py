from pydantic import BaseModel
from typing import List, Literal

class ReviewComment(BaseModel):
    file: str
    line: int
    severity: Literal["BLOCKING", "WARNING", "SUGGESTION"]
    category: Literal["bug", "security", "style", "error_handling"]
    confidence: float = 0.5  # 0.0 to 1.0, defaults to 0.5 if Claude omits it
    message: str
    suggestion: str | None = None

class ReviewResult(BaseModel):
    comments: List[ReviewComment]
    summary: str
