"""Guard the native ERPNext Project notes contract without provisioning a duplicate field."""
from __future__ import annotations


class NativeProjectNotesError(ValueError):
    pass


def require_native_project_notes(meta) -> None:
    """Require ERPNext Project.notes without creating an implicit HERO write path.
    Custom partner-notes mappings require separate provisioning approval."""
    field = meta.get_field("notes") if meta else None
    if field is None:
        raise NativeProjectNotesError("ERPNext Project.notes is required for local project notes.")
    if bool(getattr(field, "read_only", False)) or bool(getattr(field, "hidden", False)):
        raise NativeProjectNotesError("ERPNext Project.notes must remain editable.")
