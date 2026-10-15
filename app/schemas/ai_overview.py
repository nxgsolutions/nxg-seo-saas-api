from pydantic import BaseModel


class AiOverviewResponse(BaseModel):
    scope: str
    key: str
    summary: str
    bullets: list[str] = []  # the overview as 2-5 short bullet points
    source: str              # 'ai' (local model rewrite) | 'rules' (deterministic draft)
    model: str | None = None
    generated_at: str
    cached: bool
