import logging
from datetime import datetime
from enum import StrEnum

from keboola.component.exceptions import UserException
from keboola.utils.date import parse_datetime_interval
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    field_validator,
    model_validator,
)


class PrimaryKeyMode(StrEnum):
    """Legacy values accepted from rows saved before the primary-key UI simplification."""

    auto = "auto"
    custom = "custom"
    none = "none"


class LoadType(StrEnum):
    """How each evidence is written to Keboola Storage.

    ``incremental_load`` (the default) upserts records on the primary key;
    ``full_load`` overwrites the table. Both fetch the same Date Start / Date End
    window over the configured date field — there is no stateful watermark.
    """

    full_load = "full_load"
    incremental_load = "incremental_load"


# ---------------------------------------------------------------------------
# SSH tunnel sub-model
# ---------------------------------------------------------------------------


class SshTunnelKeys(BaseModel):
    """Key material produced by the Keboola ``ssh-editor`` widget.

    The widget stores the public half as ``public`` and the private half under
    the encrypted alias ``#private``.  We only need the private key at runtime.
    """

    model_config = ConfigDict(populate_by_name=True)

    public: str = ""
    # The private key is stored under the encrypted alias "#private" by the
    # Keboola ssh-editor widget.
    private_key: str = Field(default="", alias="#private")


class SshTunnelConfig(BaseModel):
    """Configuration for the optional SSH bastion-host tunnel.

    The ``ssh-editor`` widget in the Keboola UI emits an object shaped like::

        {
          "enabled": true,
          "keys": {"public": "...", "#private": "..."},
          "sshHost": "bastion.example.com",
          "user": "keboola",
          "sshPort": 22
        }

    All fields except ``enabled`` are optional at the *schema* level so the
    widget can save a half-filled form.  When ``enabled`` is ``True`` we
    validate that the minimum required fields are present and raise a clear
    :class:`~keboola.component.exceptions.UserException` instead of letting
    the tunnel attempt fail with a cryptic error.
    """

    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = False
    # Nested key object; defaults to empty so parsing never fails when the
    # user saves the config with tunnel disabled.
    keys: SshTunnelKeys = Field(default_factory=SshTunnelKeys)
    # SSH bastion host address (hostname or IP).
    ssh_host: str = Field(default="", alias="sshHost")
    # OS user on the bastion that owns the authorised_keys entry.
    user: str = ""
    # Standard SSH port; almost always 22 but configurable for non-standard setups.
    ssh_port: int = Field(default=22, alias="sshPort")

    @property
    def private_key(self) -> str:
        """Convenience accessor for the PEM private key string."""
        return self.keys.private_key

    @model_validator(mode="after")
    def _validate_enabled_fields(self) -> "SshTunnelConfig":
        """When enabled, require sshHost, user, and the private key."""
        if not self.enabled:
            return self
        missing: list[str] = []
        if not self.ssh_host:
            missing.append("sshHost")
        if not self.user:
            missing.append("user")
        if not self.keys.private_key:
            missing.append("keys.#private (private key)")
        if missing:
            raise ValueError(
                f"SSH tunnel is enabled but the following required fields are missing: "
                f"{', '.join(missing)}. "
                "Please fill in all SSH tunnel fields in the configuration."
            )
        return self


# ---------------------------------------------------------------------------
# Root + row configuration
# ---------------------------------------------------------------------------


class Configuration(BaseModel):
    """Merged root + row configuration the component receives at runtime."""

    model_config = ConfigDict(populate_by_name=True)

    # --- connection (root config) ---
    base_url: str
    company: str
    username: str
    password: str = Field(alias="#password")
    ssl_verify: bool = True
    # Optional SSH tunnel for reaching on-prem FlexiBee servers behind firewalls.
    # Absent (None) when not configured; present but enabled=False when configured
    # but switched off — both cases bypass the tunnel entirely.
    ssh_tunnel: SshTunnelConfig | None = None

    # --- evidence (row config) ---
    # Optional so sync actions (testConnection, listEvidences) can run at config
    # time before a row's evidence is selected. run() guards that it is set.
    evidence: str = ""
    # Default to incremental per Keboola convention (Storage upsert on the primary key).
    load_type: LoadType = LoadType.incremental_load
    # Which date/datetime column the Date Start / Date End window applies to.
    # Empty coerces to "lastUpdate" at runtime (see Component). Chosen via the
    # getDateFields sync action, but free-text is allowed (creatable UI field).
    date_field: str = "lastUpdate"
    date_from: str = ""
    date_to: str = ""
    detail: str = "full"
    custom_fields: str = ""
    custom_filter: str = ""
    limit: int = Field(default=200, gt=0)
    # Empty => auto-detect the key from the evidence metadata (see _resolve_primary_key).
    # Non-empty => use these columns verbatim. Creatable UI field: values may be picked
    # from getEvidenceColumns or typed by hand, and arrive as a list or a CSV string.
    primary_key: list[str] = Field(default_factory=list)
    # Kept out of the UI but accepted from saved rows created before the primary-key
    # selector was simplified. In particular, `none` must not silently become auto.
    primary_key_mode: PrimaryKeyMode | None = Field(default=None, exclude=True)

    @field_validator("primary_key", mode="before")
    @classmethod
    def _split_primary_key(cls, value):
        """Accept a comma-separated string as well as the UI's list of columns."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @computed_field
    @property
    def incremental(self) -> bool:
        """True when the row loads incrementally (output-mapping upsert on the primary key)."""
        return self.load_type == LoadType.incremental_load

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            # Build a human-readable location like "ssh_tunnel.sshHost" for
            # nested fields; fall back to just the first segment for simple ones.
            messages = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Validation Error: {', '.join(messages)}")
        logging.debug("Configuration loaded for evidence '%s'", self.evidence)

    def resolve_window(self) -> tuple[datetime | None, datetime | None]:
        """Resolve date_from/date_to strings into datetimes for the WQL window.

        Empty `date_from` => no lower bound (None).
        Empty `date_to` => **no upper bound** (None), so future-dated records are
        included. Relative ("5 days ago") and absolute ("2026-05-01") strings are
        accepted; write "today" as Date End to stop at the current moment.
        """
        if not self.date_from:
            if self.date_to:
                logging.warning("date_to is set but date_from is empty; ignoring date_to and extracting full history.")
            return None, None

        # An empty Date End used to resolve to "now", which silently excluded every
        # record dated ahead of the run — invisible to the user, who sees an empty
        # field and reasonably reads it as "no limit" [SUPPORT-17334]. Documents are
        # routinely future-dated (due dates, planned entries), so those rows just went
        # missing. Empty now means unbounded; "today" expresses the old behaviour.
        #
        # parse_datetime_interval requires two ends, so the lower bound is resolved by
        # pairing it with itself. That also keeps a *future* date_from valid — pairing
        # it with "now" is rejected with "start_date cannot exceed end_date", even
        # though "from 2030 onwards" is a perfectly sensible unbounded window.
        upper = self.date_to or self.date_from
        try:
            start, end = parse_datetime_interval(self.date_from, upper)
        except Exception as exc:
            shown = self.date_to or "(unbounded)"
            raise UserException(f"Invalid date range: date_from='{self.date_from}', date_to='{shown}': {exc}")
        return start, (end if self.date_to else None)
