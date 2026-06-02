"""SSH tunnel context manager for ABRA Flexi (FlexiBee) extractor.

On-prem ABRA Flexi servers sit behind firewalls that are not reachable from
Keboola directly.  An SSH tunnel (bastion host) lets us forward a local port
to the remote FlexiBee port through a machine that *is* reachable.

Usage::

    with SshTunnel(cfg.ssh_tunnel, cfg.base_url) as (tunnel_base_url, original_host):
        client = FlexiBeeClient(base_url=tunnel_base_url, ...,
                                tunnel_original_host=original_host)
        ...  # client automatically handles TLS + Host header

When the tunnel is disabled (``cfg.ssh_tunnel`` is None or ``enabled=False``)
the context manager is a no-op and yields ``(cfg.base_url, None)`` so the
calling code never needs to branch on whether the tunnel exists.
"""

from __future__ import annotations

import io
import logging
from contextlib import contextmanager
from urllib.parse import urlparse

import paramiko
from keboola.component.exceptions import UserException
from sshtunnel import BaseSSHTunnelForwarderError, SSHTunnelForwarder

from configuration import SshTunnelConfig

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_base_url(base_url: str) -> tuple[str, str, int]:
    """Parse *base_url* into (scheme, host, port).

    Default ports follow HTTP convention: 443 for https, 80 for http.
    An explicit port in the URL always wins (e.g. ``https://host:5434``).

    Returns:
        (scheme, host, port) — e.g. ("https", "flexi.example.com", 5434)

    Raises:
        UserException: if the URL is missing the scheme or host.
    """
    parsed = urlparse(base_url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if not scheme or not host:
        raise UserException(
            f"Cannot parse SSH tunnel target from base_url '{base_url}'. "
            "The URL must include a scheme (https:// or http://) and a hostname."
        )
    # urlparse returns None for netloc-less URLs and 0 when no port is given.
    port = parsed.port
    if not port:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


def _load_private_key(key_str: str) -> paramiko.PKey:
    """Load an SSH private key from a PEM string.

    Tries RSA first, then Ed25519, then ECDSA, and finally generic DSS so we
    are not sensitive to the key type chosen by the user.

    Raises:
        UserException: if the string is not a valid private key.
    """
    # DSSKey was removed in paramiko 5.x; only include it when present so the
    # list stays forward-compatible if it reappears, and backward-compatible
    # with older paramiko builds.
    key_types = [
        paramiko.RSAKey,
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
    ]
    if hasattr(paramiko, "DSSKey"):
        key_types.append(paramiko.DSSKey)
    last_exc: Exception | None = None
    for key_cls in key_types:
        try:
            return key_cls.from_private_key(io.StringIO(key_str))
        except paramiko.SSHException as exc:
            last_exc = exc
        except Exception as exc:  # noqa: BLE001 — paramiko may raise ValueError etc.
            last_exc = exc

    raise UserException(
        f"Could not load the SSH private key. Supported key types: RSA, Ed25519, ECDSA, DSS. Last error: {last_exc}"
    )


# ---------------------------------------------------------------------------
# Public context manager
# ---------------------------------------------------------------------------


@contextmanager
def open_tunnel(ssh_cfg: SshTunnelConfig, base_url: str):
    """Open (or skip) an SSH tunnel and yield ``(effective_base_url, original_host_or_none)``.

    When the tunnel is *not* enabled this is a pure pass-through: the original
    ``base_url`` is yielded unchanged and ``original_host`` is ``None`` so the
    caller can pass it straight through to :class:`FlexiBeeClient` without
    branching.

    When the tunnel *is* enabled:

    1. The remote bind target is derived from ``base_url`` (scheme, host, port).
    2. An ephemeral local port is chosen by the OS (``local_bind_address=('127.0.0.1', 0)``).
    3. ``effective_base_url`` is rewritten to ``<scheme>://127.0.0.1:<local_port>``
       so :class:`FlexiBeeClient` connects to the local side of the tunnel.
    4. ``original_host`` is the real ABRA Flexi hostname; the caller must pass
       it to :class:`FlexiBeeClient` so TLS certificate validation still uses
       the real hostname (via ``requests_toolbelt`` ``HostHeaderSSLAdapter``).

    All SSH/paramiko/sshtunnel errors are wrapped into :class:`UserException`
    with actionable messages.

    Args:
        ssh_cfg: Parsed :class:`~configuration.SshTunnelConfig` (may be None).
        base_url: The raw ``base_url`` from the component configuration.

    Yields:
        tuple[str, str | None]: ``(effective_base_url, original_host_or_none)``
    """
    # Fast path — tunnel disabled or not configured.
    if ssh_cfg is None or not ssh_cfg.enabled:
        logging.debug("SSH tunnel disabled; connecting directly to %s", base_url)
        yield base_url, None
        return

    # --- Resolve where to tunnel to ---
    scheme, remote_host, remote_port = _parse_base_url(base_url)
    logging.info(
        "Opening SSH tunnel: bastion=%s:%d → remote=%s:%d",
        ssh_cfg.ssh_host,
        ssh_cfg.ssh_port,
        remote_host,
        remote_port,
    )

    # --- Load the private key from the stored PEM string ---
    pkey = _load_private_key(ssh_cfg.private_key)

    # --- Build and start the forwarder ---
    forwarder = SSHTunnelForwarder(
        # Bastion host address and port.
        ssh_address_or_host=(ssh_cfg.ssh_host, ssh_cfg.ssh_port),
        ssh_username=ssh_cfg.user,
        # Use the decoded paramiko key object directly so sshtunnel never
        # has to touch the filesystem (no temp files with key material).
        ssh_pkey=pkey,
        # We tunnel to the actual FlexiBee host and port as seen from the bastion.
        remote_bind_address=(remote_host, remote_port),
        # Port 0 asks the OS to assign a free ephemeral port on 127.0.0.1.
        local_bind_address=("127.0.0.1", 0),
        # Keep the forwarded connection alive; FlexiBee runs are not instant.
        set_keepalive=30.0,
    )

    try:
        forwarder.start()
    except BaseSSHTunnelForwarderError as exc:
        _raise_tunnel_error(exc)
    except Exception as exc:  # noqa: BLE001 — paramiko can raise bare Exception
        _raise_tunnel_error(exc)

    local_port: int = forwarder.local_bind_port
    # Rewrite the URL so FlexiBeeClient connects to the local tunnel endpoint.
    # The original hostname is returned separately so the caller can configure
    # TLS to validate against it (see FlexiBeeClient tunnel_original_host).
    effective_base_url = f"{scheme}://127.0.0.1:{local_port}"
    logging.info(
        "SSH tunnel open: local=%s → remote=%s:%d (original host for TLS: %s)",
        effective_base_url,
        remote_host,
        remote_port,
        remote_host,
    )

    try:
        yield effective_base_url, remote_host
    finally:
        # Always stop the tunnel, even if the caller raises an exception.
        try:
            forwarder.stop()
            logging.debug("SSH tunnel closed.")
        except Exception:  # noqa: BLE001 — best-effort cleanup; don't mask caller exc
            logging.debug("SSH tunnel stop raised an exception (ignored).", exc_info=True)


def _raise_tunnel_error(exc: Exception) -> None:
    """Convert SSH/tunnel exceptions into actionable :class:`UserException` messages."""
    msg = str(exc).lower()
    if "authentication" in msg or "auth" in msg or "no authentication" in msg:
        raise UserException(
            f"SSH tunnel authentication failed. Check that the SSH user and private key are correct. Detail: {exc}"
        ) from exc
    if "connect" in msg or "refused" in msg or "timed out" in msg or "unreachable" in msg:
        raise UserException(
            f"SSH tunnel could not reach the bastion host. "
            "Check that the host address and port are reachable from Keboola. "
            f"Detail: {exc}"
        ) from exc
    raise UserException(f"SSH tunnel error: {exc}") from exc
