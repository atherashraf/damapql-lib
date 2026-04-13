from typing import Optional, List
from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginType(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    profile_name: str
    profile_email: str
    profile_contact: Optional[str] = None
    organization: Optional[str] = None
    department: Optional[str] = None
    designation: Optional[str] = None
    roles: List[str] = Field(default_factory=list)


class UserInfo(BaseModel):
    id: int
    username: str
    is_superuser: bool
    is_staff: bool
    name: Optional[str] = None
    email: Optional[str] = None
    contact: Optional[str] = None
    organization: Optional[str] = None
    department: Optional[str] = None
    is_active: bool = False
    is_verified: bool = False
    roles: List[str] = Field(default_factory=list)

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=lambda s: s.split('_')[0].lower() + ''.join(
            word.capitalize() for word in s.split('_')[1:]
        ),
    )


class ProfileType(BaseModel):
    id: int
    name: str
    email: str
    contact: Optional[str] = None
    organization_id: Optional[int] = None
    organization: Optional[str] = None
    department_id: Optional[int] = None
    department: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class OrganizationType(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


class DepartmentType(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    is_active: bool = True
    organization_id: int
    organization: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class RoleType(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


class EmailValidator(BaseModel):
    email: EmailStr