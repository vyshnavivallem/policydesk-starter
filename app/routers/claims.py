"""Claims API.

POST /api/claims             -> file a claim on a policy (201)
GET  /api/claims             -> list claims, newest first
GET  /api/claims/{claim_id}  -> one claim (404 if missing)
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.db import get_session
from app.models import (
    Claim,
    ClaimCreate,
    ClaimRead,
    ClaimStatus,
    ClaimStatusUpdate,
    Policy,
    PolicyStatus,
    ProductCode,
)

router = APIRouter(prefix="/api/claims", tags=["claims"])


def approved_total(session: Session, policy_id: int) -> float:
    """Return the total amount of approved claims for a policy."""
    amounts = session.exec(
        select(Claim.amount).where(
            Claim.policy_id == policy_id,
            Claim.status == ClaimStatus.APPROVED,
        )
    ).all()
    return float(sum(amounts)) if amounts else 0.0


def remaining_cover(session: Session, policy: Policy) -> float:
    """Return the remaining cover for a policy."""
    return policy.sum_insured - approved_total(session, policy.id)


def file_claim(payload: ClaimCreate, session: Session) -> Claim:
    """Validate and save a claim."""
    policy = session.get(Policy, payload.policy_id)

    if not policy:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Policy not found",
        )

    if policy.status == PolicyStatus.CANCELLED:
        claim = Claim.model_validate(payload)
        claim.status = ClaimStatus.REJECTED
        claim.reason = "Policy is cancelled"
        session.add(claim)
        session.commit()
        session.refresh(claim)
        return claim

    if policy.status != PolicyStatus.ACTIVE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Policy is {policy.status.value.lower()}; "
            "only Active policies accept claims",
        )

    if not (policy.start_date <= payload.incident_date <= policy.end_date):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Incident date must fall within the policy period "
            f"{policy.start_date} to {policy.end_date}",
        )

    remaining = remaining_cover(session, policy)

    if payload.amount > remaining:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Claim amount exceeds remaining cover of {remaining:,.2f}",
        )

    if (
        policy.product.code == ProductCode.MOTOR
        and not (payload.vehicle_registration or "").strip()
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Motor claims need a vehicle registration number",
        )

    claim = Claim.model_validate(payload)
    session.add(claim)
    session.commit()
    session.refresh(claim)
    return claim


@router.get("", response_model=list[ClaimRead])
def list_claims(
    policy_id: int | None = None,
    status: ClaimStatus | None = None,
    session: Session = Depends(get_session),
):
    stmt = select(Claim).order_by(Claim.created_at.desc())

    if policy_id is not None:
        stmt = stmt.where(Claim.policy_id == policy_id)

    if status is not None:
        stmt = stmt.where(Claim.status == status)

    return session.exec(stmt).all()


@router.post(
    "",
    response_model=ClaimRead,
    status_code=status.HTTP_201_CREATED,
)
def create_claim(
    payload: ClaimCreate,
    session: Session = Depends(get_session),
):
    return file_claim(payload, session)


@router.get("/{claim_id}", response_model=ClaimRead)
def get_claim(
    claim_id: int,
    session: Session = Depends(get_session),
):
    claim = session.get(Claim, claim_id)

    if not claim:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Claim not found",
        )

    return claim


@router.patch("/{claim_id}/status", response_model=ClaimRead)
def update_claim_status(
    claim_id: int,
    payload: ClaimStatusUpdate,
    session: Session = Depends(get_session),
):
    claim = session.get(Claim, claim_id)

    if not claim:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Claim not found",
        )

    current = claim.status
    new_status = payload.status

    allowed_moves = {
        ClaimStatus.FILED: {
            ClaimStatus.UNDER_REVIEW,
            ClaimStatus.REJECTED,
        },
        ClaimStatus.UNDER_REVIEW: {
            ClaimStatus.APPROVED,
            ClaimStatus.REJECTED,
        },
    }

    if new_status not in allowed_moves.get(current, set()):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot change claim from {current.value} to {new_status.value}",
        )

    if new_status == ClaimStatus.APPROVED:
        remaining = remaining_cover(session, claim.policy)

        if claim.amount > remaining:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Claim amount exceeds remaining cover of {remaining:,.2f}",
            )

    claim.status = new_status

    if payload.reason is not None:
        claim.reason = payload.reason

    session.add(claim)
    session.commit()
    session.refresh(claim)

    return claim