# Employee migration

- Start through Synchronisation → Employees; Full Migration starts with employees.
- Source identities: partner `id` binds Employee; `user_id` and `user.id` bind User and must agree.
- Employee company comes from ERPNext defaults or the sole configured Company; ambiguous company selection blocks intake.
- New active users receive the Employee role without a welcome email; HERO roles and account types do not grant local privileges.
- Existing User roles and local assignments are preserved; deleted HERO users remain historical references.
- Contact email and login email are separate; existing source-bound users are not renamed or merged on email changes.
- Employee gender and birth date are optional; installer and migration use standard Property Setters.
- Source fields and transformations: [`employee_mapping.json`](../hero_integration/config/employee_mapping.json).
- Employee, User, source links and checkpoint are committed per record; failed records can be retried.
- Missing source fields are omitted; ambiguous identities and incompatible local values require review.
