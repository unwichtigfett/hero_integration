"""One-time: read-only HERO Assignee field on Task."""


def execute():
    from hero_integration.integrations.task_form import setup_task_form

    setup_task_form()
