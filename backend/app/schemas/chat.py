from pydantic import BaseModel, ConfigDict, Field

SESSION_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"


class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=4000, examples=["Hello"])
    #: Optional conversation ID for follow-ups ("pause it"). Omit to use the default session.
    session_id: str | None = Field(default=None, pattern=SESSION_ID_PATTERN)


class ChatResponse(BaseModel):
    response: str
    session_id: str | None = None
    tools_used: list[str] | None = None
