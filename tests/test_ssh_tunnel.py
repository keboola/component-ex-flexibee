"""Unit tests for SSH tunnel support.

Tests cover three areas:
1. base_url parsing (scheme, host, port extraction)
2. SshTunnelConfig pydantic model parsing and validation
3. open_tunnel context manager wiring with SSHTunnelForwarder mocked

No live SSH connections are made; sshtunnel and paramiko are fully mocked.
The existing VCR / functional tests are unaffected because they never set
ssh_tunnel in the config, so the no-tunnel path is exercised as before.
"""

from __future__ import annotations

from unittest import mock

import pytest

# -----------------------------------------------------------------------
# 1. base_url parsing
# -----------------------------------------------------------------------


class TestParseBaseUrl:
    """_parse_base_url converts base_url strings to (scheme, host, port)."""

    def setup_method(self):
        # Import here so the PYTHONPATH (src/) is already in effect via pytest.
        from client.ssh_tunnel import _parse_base_url

        self.parse = _parse_base_url

    def test_https_default_port(self):
        scheme, host, port = self.parse("https://demo.flexibee.eu")
        assert scheme == "https"
        assert host == "demo.flexibee.eu"
        assert port == 443

    def test_http_default_port(self):
        scheme, host, port = self.parse("http://internal.corp")
        assert scheme == "http"
        assert host == "internal.corp"
        assert port == 80

    def test_explicit_port_wins(self):
        scheme, host, port = self.parse("https://flexi.example.com:5434")
        assert scheme == "https"
        assert host == "flexi.example.com"
        assert port == 5434

    def test_explicit_port_http(self):
        scheme, host, port = self.parse("http://10.0.0.5:8080")
        assert scheme == "http"
        assert host == "10.0.0.5"
        assert port == 8080

    def test_trailing_slash_ignored(self):
        scheme, host, port = self.parse("https://flexi.example.com/")
        assert host == "flexi.example.com"
        assert port == 443

    def test_missing_scheme_raises_user_exception(self):
        from keboola.component import UserException

        with pytest.raises(UserException, match="scheme"):
            self.parse("flexi.example.com")


# -----------------------------------------------------------------------
# 2. SshTunnelConfig model parsing and validation
# -----------------------------------------------------------------------


class TestSshTunnelConfig:
    """SshTunnelConfig parsing, defaults, and enabled-but-missing-fields errors."""

    def test_disabled_by_default(self):
        from configuration import SshTunnelConfig

        cfg = SshTunnelConfig()
        assert cfg.enabled is False
        assert cfg.ssh_port == 22

    def test_full_enabled_config(self):
        from configuration import SshTunnelConfig

        raw = {
            "enabled": True,
            "keys": {"public": "ssh-rsa AAAA...", "#private": "-----BEGIN RSA PRIVATE KEY-----\n..."},
            "sshHost": "bastion.example.com",
            "user": "keboola",
            "sshPort": 2222,
        }
        cfg = SshTunnelConfig(**raw)
        assert cfg.enabled is True
        assert cfg.ssh_host == "bastion.example.com"
        assert cfg.user == "keboola"
        assert cfg.ssh_port == 2222
        # The private key is accessible via the convenience property.
        assert cfg.private_key == "-----BEGIN RSA PRIVATE KEY-----\n..."
        assert cfg.keys.public == "ssh-rsa AAAA..."

    def test_private_key_alias_mapping(self):
        """keys.#private must be accessible via cfg.private_key."""
        from configuration import SshTunnelConfig

        raw = {
            "enabled": False,
            "keys": {"#private": "my-key-pem"},
        }
        cfg = SshTunnelConfig(**raw)
        assert cfg.private_key == "my-key-pem"

    def test_enabled_missing_ssh_host_raises(self):
        """enabled=True without sshHost, user, or key → ValidationError → UserException."""
        from keboola.component import UserException

        from configuration import Configuration

        raw = {
            "base_url": "https://flexi.example.com",
            "company": "demo",
            "username": "u",
            "#password": "p",
            "ssh_tunnel": {
                "enabled": True,
                # sshHost, user, and private key deliberately omitted
                "keys": {},
            },
        }
        with pytest.raises(UserException, match="sshHost"):
            Configuration(**raw)

    def test_enabled_missing_user_and_key_raises(self):
        """All three missing fields appear in the error message."""
        from keboola.component import UserException

        from configuration import Configuration

        raw = {
            "base_url": "https://flexi.example.com",
            "company": "demo",
            "username": "u",
            "#password": "p",
            "ssh_tunnel": {
                "enabled": True,
                "sshHost": "bastion.example.com",
                # user and private key omitted
                "keys": {},
            },
        }
        with pytest.raises(UserException, match="user"):
            Configuration(**raw)

    def test_disabled_tunnel_allows_missing_fields(self):
        """When enabled=False the required-fields validation is skipped."""
        from configuration import Configuration

        raw = {
            "base_url": "https://flexi.example.com",
            "company": "demo",
            "username": "u",
            "#password": "p",
            "ssh_tunnel": {
                "enabled": False,
                # No other fields — should be fine
            },
        }
        cfg = Configuration(**raw)
        assert cfg.ssh_tunnel is not None
        assert cfg.ssh_tunnel.enabled is False

    def test_no_ssh_tunnel_key_gives_none(self):
        """ssh_tunnel absent from config → Configuration.ssh_tunnel is None."""
        from configuration import Configuration

        raw = {
            "base_url": "https://flexi.example.com",
            "company": "demo",
            "username": "u",
            "#password": "p",
        }
        cfg = Configuration(**raw)
        assert cfg.ssh_tunnel is None


# -----------------------------------------------------------------------
# 3. open_tunnel context manager wiring
# -----------------------------------------------------------------------


class TestOpenTunnel:
    """open_tunnel wires SSHTunnelForwarder correctly and cleans up reliably."""

    def _make_ssh_cfg(self, **overrides):
        """Build a minimal enabled SshTunnelConfig without touching the real validator."""
        from configuration import SshTunnelConfig, SshTunnelKeys

        # Bypass the model_validator so we can pass a fake key string.
        cfg = SshTunnelConfig.model_construct(
            enabled=True,
            keys=SshTunnelKeys.model_construct(
                private_key="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"
            ),
            ssh_host="bastion.example.com",
            user="keboola",
            ssh_port=22,
        )
        for k, v in overrides.items():
            object.__setattr__(cfg, k, v)
        return cfg

    def test_disabled_tunnel_is_passthrough(self):
        """When enabled=False, base_url is yielded unchanged and original_host is None."""
        from client.ssh_tunnel import open_tunnel
        from configuration import SshTunnelConfig

        cfg = SshTunnelConfig(enabled=False)
        base_url = "https://demo.flexibee.eu"
        with open_tunnel(cfg, base_url) as (turl, thost):
            assert turl == base_url
            assert thost is None

    def test_none_tunnel_config_is_passthrough(self):
        """When ssh_cfg is None, base_url is yielded unchanged."""
        from client.ssh_tunnel import open_tunnel

        base_url = "https://demo.flexibee.eu"
        with open_tunnel(None, base_url) as (turl, thost):
            assert turl == base_url
            assert thost is None

    def test_enabled_tunnel_rewrites_base_url_and_yields_original_host(self):
        """Enabled tunnel: effective_base_url points at 127.0.0.1:<local_port>."""
        from client.ssh_tunnel import open_tunnel

        ssh_cfg = self._make_ssh_cfg()

        mock_forwarder = mock.MagicMock()
        mock_forwarder.local_bind_port = 54321

        with (
            mock.patch("client.ssh_tunnel._load_private_key", return_value=mock.MagicMock()),
            mock.patch("client.ssh_tunnel.SSHTunnelForwarder", return_value=mock_forwarder),
        ):
            with open_tunnel(ssh_cfg, "https://flexi.example.com:5434") as (turl, thost):
                assert turl == "https://127.0.0.1:54321"
                assert thost == "flexi.example.com"

    def test_forwarder_receives_correct_ssh_and_remote_params(self):
        """SSHTunnelForwarder is constructed with the correct SSH and remote-bind args."""
        from client.ssh_tunnel import open_tunnel

        ssh_cfg = self._make_ssh_cfg()
        fake_pkey = mock.MagicMock()

        mock_forwarder = mock.MagicMock()
        mock_forwarder.local_bind_port = 11111

        with (
            mock.patch("client.ssh_tunnel._load_private_key", return_value=fake_pkey),
            mock.patch("client.ssh_tunnel.SSHTunnelForwarder", return_value=mock_forwarder) as MockForwarder,
        ):
            with open_tunnel(ssh_cfg, "https://flexi.example.com"):
                pass  # just need the forwarder to be constructed

        call_kwargs = MockForwarder.call_args
        # Positional arg: (sshHost, sshPort)
        assert call_kwargs.kwargs["ssh_address_or_host"] == ("bastion.example.com", 22)
        assert call_kwargs.kwargs["ssh_username"] == "keboola"
        assert call_kwargs.kwargs["ssh_pkey"] is fake_pkey
        # Remote bind goes to the real FlexiBee host on its port.
        assert call_kwargs.kwargs["remote_bind_address"] == ("flexi.example.com", 443)
        # Local side: ephemeral port on loopback.
        assert call_kwargs.kwargs["local_bind_address"] == ("127.0.0.1", 0)

    def test_forwarder_stopped_after_context_exit(self):
        """Forwarder.stop() is always called when the context manager exits normally."""
        from client.ssh_tunnel import open_tunnel

        ssh_cfg = self._make_ssh_cfg()
        mock_forwarder = mock.MagicMock()
        mock_forwarder.local_bind_port = 22222

        with (
            mock.patch("client.ssh_tunnel._load_private_key", return_value=mock.MagicMock()),
            mock.patch("client.ssh_tunnel.SSHTunnelForwarder", return_value=mock_forwarder),
        ):
            with open_tunnel(ssh_cfg, "https://flexi.example.com"):
                pass

        mock_forwarder.stop.assert_called_once()

    def test_forwarder_stopped_even_when_body_raises(self):
        """Forwarder.stop() is called even if the body of the with-block raises."""
        from client.ssh_tunnel import open_tunnel

        ssh_cfg = self._make_ssh_cfg()
        mock_forwarder = mock.MagicMock()
        mock_forwarder.local_bind_port = 33333

        with (
            mock.patch("client.ssh_tunnel._load_private_key", return_value=mock.MagicMock()),
            mock.patch("client.ssh_tunnel.SSHTunnelForwarder", return_value=mock_forwarder),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                with open_tunnel(ssh_cfg, "https://flexi.example.com"):
                    raise RuntimeError("boom")

        mock_forwarder.stop.assert_called_once()

    def test_bad_key_raises_user_exception(self):
        """A key string that cannot be loaded raises UserException (not a raw paramiko error)."""
        from keboola.component import UserException

        from client.ssh_tunnel import open_tunnel

        ssh_cfg = self._make_ssh_cfg()

        # _load_private_key is NOT mocked here — it receives the fake key
        # string and will fail to parse it, which should surface as UserException.
        with pytest.raises(UserException, match="private key"):
            with open_tunnel(ssh_cfg, "https://flexi.example.com"):
                pass  # should not reach here

    def test_tunnel_start_failure_raises_user_exception(self):
        """sshtunnel connection errors are wrapped into UserException."""
        from keboola.component import UserException
        from sshtunnel import BaseSSHTunnelForwarderError

        from client.ssh_tunnel import open_tunnel

        ssh_cfg = self._make_ssh_cfg()
        mock_forwarder = mock.MagicMock()
        mock_forwarder.start.side_effect = BaseSSHTunnelForwarderError("Connection refused")

        with (
            mock.patch("client.ssh_tunnel._load_private_key", return_value=mock.MagicMock()),
            mock.patch("client.ssh_tunnel.SSHTunnelForwarder", return_value=mock_forwarder),
        ):
            with pytest.raises(UserException):
                with open_tunnel(ssh_cfg, "https://flexi.example.com"):
                    pass
