from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ApprovalRequirement(StrEnum):
    NOT_REQUIRED = "not_required"
    REQUIRED = "required"


@dataclass(frozen=True, slots=True)
class MutationRecommendation:
    action: str
    payload: dict[str, str]
    rationale: str


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class MutationResult:
    action: str
    status: str
    audit_reference: str


class MutationAuthorizer(Protocol):
    async def authorize(
        self, recommendation: MutationRecommendation, actor: str
    ) -> AuthorizationDecision: ...


class ApprovalPolicy(Protocol):
    def requirement_for(self, recommendation: MutationRecommendation) -> ApprovalRequirement: ...


class MutationExecutor(Protocol):
    async def execute(self, recommendation: MutationRecommendation) -> MutationResult: ...


class MutationAudit(Protocol):
    async def record(
        self, recommendation: MutationRecommendation, result: MutationResult
    ) -> None: ...


class MutationCoordinator:
    """Keeps recommendations separate from authorized, approved execution."""

    def __init__(
        self,
        authorizer: MutationAuthorizer,
        approval_policy: ApprovalPolicy,
        executor: MutationExecutor,
        audit: MutationAudit,
    ) -> None:
        self._authorizer = authorizer
        self._approval_policy = approval_policy
        self._executor = executor
        self._audit = audit

    async def execute(
        self,
        recommendation: MutationRecommendation,
        *,
        actor: str,
        approval_granted: bool = False,
    ) -> MutationResult:
        decision = await self._authorizer.authorize(recommendation, actor)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        if (
            self._approval_policy.requirement_for(recommendation) == ApprovalRequirement.REQUIRED
            and not approval_granted
        ):
            raise PermissionError("Human approval is required before execution")
        result = await self._executor.execute(recommendation)
        await self._audit.record(recommendation, result)
        return result