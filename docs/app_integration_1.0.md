# ERPNext, Helpdesk and CRM

- ERPNext owns canonical `Customer`, `Contact`, `Address`, `Project`, `Task` and `Event` records.
- HERO identities are source-instance-bound through `HERO Integration Link`; names alone do not prove identity.
- Imported projects and locally created ERPNext projects may coexist; local projects are not automatically merged with HERO records.
- Helpdesk tickets and CRM leads/deals reference ERPNext through `erp_customer` and `erp_project`.
- Project/customer mismatches are rejected; contact-to-customer inference requires a unique native Dynamic Link.
- Integration guards prevent duplicate Helpdesk/CRM customer mirrors and protect bound records from uncontrolled deletion.
- HERO deletion does not automatically delete native records; unresolved relations remain available for review.
- Task, event, assignment and outbound operations require their own mapping, permission and approval contracts.
