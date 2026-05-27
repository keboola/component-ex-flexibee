import logging

from keboola.component.exceptions import UserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Configuration(BaseModel):
    """Merged root + row configuration the component receives at runtime."""

    model_config = ConfigDict(populate_by_name=True)

    # --- connection (root config) ---
    base_url: str
    company: str
    username: str
    password: str = Field(alias="#password")
    ssl_verify: bool = True

    # --- evidence (row config) ---
    evidence: str
    date_from: str = ""
    date_to: str = ""
    detail: str = "full"
    custom_fields: str = ""
    custom_filter: str = ""
    limit: int = 200

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            messages = [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Validation Error: {', '.join(messages)}")
        logging.debug("Configuration loaded for evidence '%s'", self.evidence)
