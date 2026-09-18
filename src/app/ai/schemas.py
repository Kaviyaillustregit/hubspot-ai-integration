from pydantic import BaseModel, ConfigDict


class StructuredAIOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
