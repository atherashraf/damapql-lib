from typing import Optional

from fastapi import HTTPException

from app.damapql.auth.models import Department, Organization


def resolve_or_create_organization(
    session,
    organization_label: Optional[str],
    dist_id: Optional[int],
    dist_name: Optional[str],
) -> Optional[Organization]:
    label = organization_label.strip() if organization_label else None
    gis_name = dist_name.strip() if dist_name else None

    if not label and dist_id is None and not gis_name:
        return None

    organization_obj = None

    if dist_id is not None:
        organization_obj = (
            session.query(Organization)
            .filter(Organization.dist_id == dist_id)
            .first()
        )

    if not organization_obj and label:
        organization_obj = (
            session.query(Organization)
            .filter(Organization.name == label)
            .first()
        )

    if not organization_obj and gis_name:
        organization_obj = (
            session.query(Organization)
            .filter(Organization.dist_name == gis_name)
            .first()
        )

    if organization_obj:
        if dist_id is not None:
            organization_obj.dist_id = dist_id
        if gis_name:
            organization_obj.dist_name = gis_name
        elif label and not organization_obj.dist_name:
            organization_obj.dist_name = label
        organization_obj.is_active = True
        return organization_obj

    final_name = label or gis_name
    if not final_name:
        raise HTTPException(status_code=400, detail="District name is required.")

    organization_obj = Organization(
        name=final_name,
        dist_id=dist_id,
        dist_name=gis_name or final_name,
        is_active=True,
    )
    session.add(organization_obj)
    session.flush()
    return organization_obj


def resolve_or_create_department(
    session,
    organization_obj: Optional[Organization],
    department_label: Optional[str],
    subdiv_id: Optional[int],
    subdiv_name: Optional[str],
) -> Optional[Department]:
    label = department_label.strip() if department_label else None
    gis_name = subdiv_name.strip() if subdiv_name else None

    if not label and subdiv_id is None and not gis_name:
        return None

    if not organization_obj:
        raise HTTPException(
            status_code=400,
            detail="Subdivision cannot be assigned without district."
        )

    department_obj = None

    if subdiv_id is not None:
        department_obj = (
            session.query(Department)
            .filter(
                Department.organization_id == organization_obj.id,
                Department.subdiv_id == subdiv_id,
            )
            .first()
        )

    if not department_obj and label:
        department_obj = (
            session.query(Department)
            .filter(
                Department.organization_id == organization_obj.id,
                Department.name == label.lower(),
            )
            .first()
        )

    if not department_obj and gis_name:
        department_obj = (
            session.query(Department)
            .filter(
                Department.organization_id == organization_obj.id,
                Department.subdiv_name == gis_name,
            )
            .first()
        )

    if department_obj:
        if subdiv_id is not None:
            department_obj.subdiv_id = subdiv_id
        if gis_name:
            department_obj.subdiv_name = gis_name
        elif label and not department_obj.subdiv_name:
            department_obj.subdiv_name = label
        department_obj.is_active = True
        return department_obj

    final_name = label or gis_name
    if not final_name:
        raise HTTPException(status_code=400, detail="Subdivision name is required.")

    department_obj = Department(
        name=final_name,
        organization_id=organization_obj.id,
        subdiv_id=subdiv_id,
        subdiv_name=gis_name or final_name,
        is_active=True,
    )
    session.add(department_obj)
    session.flush()
    return department_obj