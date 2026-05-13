from typing import Optional, List
from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginType(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    profile_name: str
    profile_email: EmailStr
    profile_contact: Optional[str] = None
    designation: Optional[str] = None

    dist_id: Optional[int] = None
    dist_name: Optional[str] = None

    wasa_div_id: Optional[int] = None
    wasa_div_name: Optional[str] = None

    wasa_subdiv_id: Optional[int] = None
    wasa_subdiv_name: Optional[str] = None

    password: Optional[str] = None
    roles: List[str] = Field(default_factory=list)


class UserUpdate(BaseModel):
    profile_name: Optional[str] = None
    profile_email: Optional[EmailStr] = None
    profile_contact: Optional[str] = None
    designation: Optional[str] = None

    dist_id: Optional[int] = None
    dist_name: Optional[str] = None

    wasa_div_id: Optional[int] = None
    wasa_div_name: Optional[str] = None

    wasa_subdiv_id: Optional[int] = None
    wasa_subdiv_name: Optional[str] = None

    roles: Optional[List[str]] = None


class UserInfo(BaseModel):
    id: int
    username: str
    is_superuser: bool
    is_staff: bool

    name: Optional[str] = None
    email: Optional[str] = None
    contact: Optional[str] = None
    designation: Optional[str] = None

    dist_id: Optional[int] = None
    dist_name: Optional[str] = None

    wasa_div_id: Optional[int] = None
    wasa_div_name: Optional[str] = None

    wasa_subdiv_id: Optional[int] = None
    wasa_subdiv_name: Optional[str] = None

    is_active: bool = False
    is_verified: bool = False
    roles: List[str] = Field(default_factory=list)

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=lambda s: s.split('_')[0].lower() + ''.join(
            word.capitalize() for word in s.split('_')[1:]
        ),
    )

    def has_role(self, role: str) -> bool:
        return role.lower() in {r.lower() for r in self.roles}

class ProfileType(BaseModel):
    id: int
    name: str
    email: str
    contact: Optional[str] = None
    designation: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class RoleType(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


class EmailValidator(BaseModel):
    email: EmailStr