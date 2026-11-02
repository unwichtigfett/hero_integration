"""Bind writeback approval to a confirmed connection without retaining secrets."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from hero_integration.client.settings import HeroConfigurationError, require_confirmed_source_binding
from hero_integration.services.frappe_writeback import WritebackContractError
from hero_integration.services.introspection import endpoint_fingerprint


@dataclass(frozen=True)
class SourceContext:
    source_instance: str
    binding_revision: int
    endpoint_fingerprint: str

    def __post_init__(self):
        if (not isinstance(self.source_instance, str) or not self.source_instance
                or self.source_instance != self.source_instance.strip()
                or type(self.binding_revision) is not int or self.binding_revision < 0
                or not isinstance(self.endpoint_fingerprint, str)
                or not re.fullmatch(r'[0-9a-f]{64}', self.endpoint_fingerprint)):
            raise WritebackContractError('Writeback requires a complete approved source context.')

    @property
    def fingerprint(self) -> str:
        payload = [self.source_instance, self.binding_revision, self.endpoint_fingerprint]
        return sha256(json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _current(configuration=None):
    if configuration is None:
        from hero_integration.client.settings import get_configuration
        configuration = get_configuration(fresh=True)
    revision = getattr(configuration, 'source_binding_revision', None)
    confirmed = getattr(configuration, 'source_binding_confirmed', None)
    try:
        require_confirmed_source_binding(revision, revision if confirmed is True else -1)
    except HeroConfigurationError as error:
        raise WritebackContractError(str(error)) from error
    source = getattr(configuration, 'source_instance', None)
    endpoint = getattr(getattr(configuration, 'endpoint', None), 'url', None)
    if (not isinstance(source, str) or not source or source != source.strip()
            or not isinstance(endpoint, str) or not endpoint):
        raise WritebackContractError('Writeback requires a complete approved source context.')
    return configuration, SourceContext(source, revision, endpoint_fingerprint(endpoint, source))


def capture_source(snapshot, expected_source: str, configuration=None) -> SourceContext:
    """Capture source proof for the exact preview and approved snapshot."""
    _configuration, current = _current(configuration)
    if (not isinstance(expected_source, str) or not expected_source
            or current.source_instance != expected_source
            or getattr(snapshot, 'source_instance', None) != expected_source):
        raise WritebackContractError('The configured HERO source does not match the approved writeback source.')
    if getattr(snapshot, 'endpoint_fingerprint', None) != current.endpoint_fingerprint:
        raise WritebackContractError('The configured HERO endpoint does not match the approved writeback snapshot.')
    return current


def validate_source(context: SourceContext, configuration=None):
    """Revalidate source, endpoint and confirmed revision immediately before I/O."""
    if not isinstance(context, SourceContext):
        raise WritebackContractError('Writeback requires a complete approved source context.')
    configuration, current = _current(configuration)
    if current != context:
        raise WritebackContractError('The HERO connection changed after the writeback preview; review it again.')
    return configuration
