"""Synthetic fleet fixture and trusted resource resolution."""

from __future__ import annotations

from verdictloop.schema import (
    Actor,
    Approval,
    Call,
    Fixture,
    ResolvedResource,
    Resource,
)

TENANTS = ("acme", "globex", "initech")
SERVICES = ("checkout", "billing", "search")
ENV_LABELS = {
    "staging": ("staging", "stg"),
    "production": ("production", "prod-us"),
}
RESTART = "restart_service"

APPROVED_CTX = "ctx-approved"
UNAPPROVED_CTX = "ctx-none"
APPROVED_INCIDENT = "INC-1"
OTHER_INCIDENT = "INC-2"
VALID_TIME = 100
EXPIRED_TIME = 300
APPROVAL_WINDOW = (0, 200)

ON_CALL = {"acme": "alice", "globex": "bob", "initech": "carol"}
OUTSIDER = "mallory"


def resource_id(tenant: str, service: str, env: str) -> str:
    return f"{tenant}/{service}/{env}"


def build_fixture() -> Fixture:
    resources = tuple(
        Resource(
            resource_id=resource_id(t, s, env),
            tenant=t,
            service=s,
            env_canonical=env,
            labels=labels,
        )
        for t in TENANTS
        for s in SERVICES
        for env, labels in ENV_LABELS.items()
    )
    actors = tuple(
        Actor(actor_id=a, tenant=t, services=SERVICES) for t, a in ON_CALL.items()
    ) + (Actor(actor_id=OUTSIDER, tenant=None),)
    # Each on-call actor holds an exact-resource approval for INC-1 on every
    # resource in their own tenant, available only in the approved context.
    approvals = tuple(
        Approval(
            approval_id=f"apr-{r.resource_id}",
            context_id=APPROVED_CTX,
            actor=ON_CALL[r.tenant],
            tool=RESTART,
            resource_id=r.resource_id,
            incident_id=APPROVED_INCIDENT,
            not_before=APPROVAL_WINDOW[0],
            expires_at=APPROVAL_WINDOW[1],
        )
        for r in resources
    )
    fixture = Fixture(
        fixture_id="fleet-v1",
        resources=resources,
        actors=actors,
        tools=(RESTART,),
        incidents=(APPROVED_INCIDENT, OTHER_INCIDENT),
        approval_contexts=(APPROVED_CTX, UNAPPROVED_CTX),
        approvals=approvals,
        eval_times=(VALID_TIME, EXPIRED_TIME),
    )
    return fixture.model_copy(update={"fixture_hash": fixture.content_hash()})


def resolve(call: Call, fixture: Fixture) -> ResolvedResource | None:
    """Resolve a raw (tenant, service, env_label) to a canonical resource.

    Fails closed (None) when the label matches no resource or is ambiguous
    within its tenant/service scope.
    """
    matches = [
        r for r in fixture.resources
        if r.tenant == call.tenant and r.service == call.service and call.env_label in r.labels
    ]
    if len(matches) != 1:
        return None
    r = matches[0]
    return ResolvedResource(
        resource_id=r.resource_id,
        tenant=r.tenant,
        service=r.service,
        env_canonical=r.env_canonical,
    )


def trusted_approvals(fixture: Fixture, approvals: list[Approval] | None, context_id: str) -> list[Approval]:
    """Only approval records that exist in the trusted fixture count."""
    trusted = fixture.approvals_for(context_id)
    if approvals is None:
        return trusted
    allowed = set(trusted)
    return [a for a in approvals if a in allowed]


_INTEGRITY: dict[int, tuple[Fixture, bool]] = {}


def fixture_is_intact(fixture: Fixture) -> bool:
    """Stored hash matches content. Cached per (immutable) fixture object."""
    cached = _INTEGRITY.get(id(fixture))
    if cached is not None and cached[0] is fixture:
        return cached[1]
    ok = bool(fixture.fixture_hash) and fixture.fixture_hash == fixture.content_hash()
    _INTEGRITY[id(fixture)] = (fixture, ok)
    return ok
