from __future__ import annotations

from dataclasses import dataclass, field

import frappe

from .graphql import HeroClientError, HeroEndpoint, validate_endpoint


class HeroConfigurationError(RuntimeError):
    pass


BINDING_FIELDS = ("hero_api_url", "hero_api_token", "hero_source_instance")


@dataclass(frozen=True)
class HeroConfiguration:
    endpoint: HeroEndpoint
    api_token: str = field(repr=False)
    timeout_seconds: int = 30
    source_instance: str = "default"
    source_binding_revision: int = 0
    source_binding_confirmed: bool = True


def next_source_binding(revision: int, confirmed_revision: int, *, binding_changed: bool, confirm: bool) -> tuple[int, int]:
    """Return binding and confirmed revisions after Settings save.
    Endpoint, token or source changes require explicit confirmation because HERO
    provides no independent account identity. Legacy bindings start at revision 0."""
    revision = int(revision or 0) + (1 if binding_changed else 0)
    confirmed_revision = revision if confirm else int(confirmed_revision or 0)
    return revision, confirmed_revision


def require_confirmed_source_binding(revision: int, confirmed_revision: int) -> None:
    """Require an explicit confirmation of the current binding revision."""
    if (type(revision) is not int or revision < 0
            or type(confirmed_revision) is not int or confirmed_revision != revision):
        raise HeroConfigurationError("The HERO endpoint, token or source instance changed; an administrator must confirm the source binding.")


CONFIGURATION_FIELDS = (
    "hero_api_url", "allowed_hero_hosts", "timeout_seconds", "hero_source_instance",
    "source_binding_revision", "source_binding_confirmed_revision",
)


def get_configuration(*, fresh: bool = False) -> HeroConfiguration:
    if fresh:
        return _fresh_configuration()
    settings = frappe.get_single("HERO Integration Settings")
    values = {name: getattr(settings, name, None) for name in CONFIGURATION_FIELDS}
    return _configuration_from_values(values, lambda: settings.get_password("hero_api_token", raise_exception=False))


def _configuration_from_values(values, token_loader):
    hosts = {line.strip().lower() for line in (values.get("allowed_hero_hosts") or "").splitlines() if line.strip()}
    try:
        endpoint = validate_endpoint(values.get("hero_api_url"), hosts)
    except HeroClientError as exc:
        raise HeroConfigurationError(str(exc)) from exc
    token = token_loader() or ""
    if not token:
        raise HeroConfigurationError("HERO API token is not configured.")
    source_instance = str(values.get("hero_source_instance") or "default").strip()
    if not source_instance:
        raise HeroConfigurationError("HERO source instance is invalid.")
    revision = int(values.get("source_binding_revision") or 0)
    return HeroConfiguration(
        endpoint=endpoint, api_token=token, timeout_seconds=int(values.get("timeout_seconds") or 30),
        source_instance=source_instance, source_binding_revision=revision,
        source_binding_confirmed=int(values.get("source_binding_confirmed_revision") or 0) == revision,
    )


def _configuration_connection():
    from frappe.database import get_db

    conf = frappe.local.conf
    if not conf.get("db_name"):
        raise HeroConfigurationError("The current HERO connection could not be verified.")
    # Always read the site's primary, never a possibly lagging request replica.
    return get_db(socket=conf.get("db_socket"), host=conf.get("db_host"), port=conf.get("db_port"),
                  user=conf.get("db_user") or conf["db_name"], password=conf.get("db_password"),
                  cur_db_name=conf["db_name"])


def _fresh_configuration():
    """Read committed connection settings and token together, outside the request snapshot."""
    try:
        connection = _configuration_connection()
        try:
            connection.begin(read_only=True)
            # One statement gives settings and credentials the same database view.
            placeholders = ", ".join(["%s"] * len(CONFIGURATION_FIELDS))
            rows = connection.sql(
                "SELECT s.field, s.value, a.password AS encrypted_token FROM `tabSingles` s "
                "LEFT JOIN `__Auth` a ON a.doctype=s.doctype AND a.name=s.doctype "
                "AND a.fieldname='hero_api_token' AND a.encrypted=1 "
                "WHERE s.doctype=%s AND s.field IN (" + placeholders + ")",
                ("HERO Integration Settings", *CONFIGURATION_FIELDS), as_dict=True,
            )
        finally:
            # Close only this read transaction, before any external request.
            connection.close()
        values = {}
        encrypted_token = None
        for row in rows:
            if row.field in values:
                raise HeroConfigurationError("The current HERO connection could not be verified.")
            values[row.field] = row.value
            encrypted_token = row.encrypted_token
        return _configuration_from_values(values, lambda: _decrypt_configuration_token(encrypted_token))
    except HeroConfigurationError:
        raise
    except Exception as error:
        raise HeroConfigurationError("The current HERO connection could not be verified.") from error


def _decrypt_configuration_token(value):
    if not value:
        return ""
    from frappe.utils.password import decrypt

    encryption_key = frappe.local.conf.get("encryption_key")
    if not encryption_key:
        raise HeroConfigurationError("The current HERO connection could not be verified.")
    return decrypt(value, encryption_key=encryption_key, key="HERO Integration Settings.hero_api_token")
