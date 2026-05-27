import logging
from datetime import datetime

from keboola.component.exceptions import UserException
from keboola.utils.date import parse_datetime_interval
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

    def resolve_window(self) -> tuple[datetime | None, datetime | None]:
        """Resolve date_from/date_to strings into datetimes for the WQL window.

        Empty `date_from` => no lower bound (None). Empty `date_to` defaults to "now".
        Relative ("5 days ago") and absolute ("2026-05-01") strings are accepted.
        """
        if not self.date_from:
            return None, None
        date_to = self.date_to or "now"
        try:
            start, end = parse_datetime_interval(self.date_from, date_to)
        except (ValueError, TypeError) as exc:
            raise UserException(
                f"Invalid date range: date_from='{self.date_from}', date_to='{date_to}': {exc}"
            )
        return start, end
