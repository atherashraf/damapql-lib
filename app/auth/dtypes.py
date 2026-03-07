from typing import Optional, List

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginType(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    profile_name: str
    profile_email: str
    profile_contact: str
    department: Optional[str] = None
    roles: List[str] = Field(default_factory=list)


class UserInfo(BaseModel):
    id: int
    username: str
    is_superuser: bool
    is_staff: bool
    name: Optional[str] = None
    email: Optional[str] = None
    contact: Optional[str] = None
    department: Optional[str] = None
    is_active: bool = False
    is_verified: bool = False
    roles: List[str] = Field(default_factory=list)

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=lambda s: s.split('_')[0].lower() + ''.join(
            word.capitalize() for word in s.split('_')[1:]
        ),
        json_schema_extra={
            "example": {
                "id": 1,
                "username": "ather",
                "is_superuser": False,
                "is_staff": True,
                "name": "Ather Ashraf",
                "email": "ather@example.com",
                "contact": "+123456789",
                "department": "survey",
                "is_active": True,
                "is_verified": True,
                "roles": ["survey_manager", "data_editor"]
            }
        }
    )


class ProfileType(BaseModel):
    id: int
    name: str
    email: str
    contact: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class RoleType(BaseModel):
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

    model_config = ConfigDict(from_attributes=True)


class EmailValidator(BaseModel):
    email: EmailStr