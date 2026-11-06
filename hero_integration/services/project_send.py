"""Public Project delivery entry points and the shared read client."""
import frappe
from hero_integration.services.localized_error import LocalizedError

SEND_ROLES = ("System Manager", "HERO Writeback")


class ProjectSendError(LocalizedError):
    """The configured project delivery could not be completed safely."""


def may_send() -> bool:
    return bool(set(SEND_ROLES) & set(frappe.get_roles()))



def _read_client():
    from hero_integration.client.graphql import HeroGraphQLClient
    from hero_integration.client.settings import get_configuration

    config = get_configuration()
    return HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds, max_retries=2)



def preview(project: str, phase_id=None, client=None) -> dict:
    from hero_integration.services.project_delivery import preview as delivery_preview
    try:
        return delivery_preview(project, phase_id=phase_id, client=client)
    except ValueError as error:
        raise ProjectSendError.from_error(error) from error



def send(project: str, phase_id=None, fingerprint: str = '', confirmed=0, client=None, mutation_client=None, delivery=None) -> dict:
    from hero_integration.services.project_delivery import send as delivery_send
    try:
        return delivery_send(project, phase_id=phase_id, fingerprint=fingerprint, delivery=delivery,
                             confirmed=confirmed, client=client, mutation_client=mutation_client)
    except ValueError as error:
        raise ProjectSendError.from_error(error) from error
