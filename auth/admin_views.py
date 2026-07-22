from sqladmin import Admin, ModelView

from app.damapql.auth.models import (
    Department,
    Organization,
    Profile,
    Role,
    User,
)


class OrganizationAdmin(ModelView, model=Organization):
    name = "Organization"
    name_plural = "Organizations"
    icon = "fa-solid fa-building"
    category = "Authentication"

    column_list = Organization.get_fields()


class DepartmentAdmin(ModelView, model=Department):
    name = "Department"
    name_plural = "Departments"
    icon = "fa-solid fa-sitemap"
    category = "Authentication"

    column_list = Department.get_fields()


class ProfileAdmin(ModelView, model=Profile):
    name = "Profile"
    name_plural = "Profiles"
    icon = "fa-solid fa-id-card"
    category = "Authentication"

    column_list = Profile.get_fields()


class RoleAdmin(ModelView, model=Role):
    name = "Role"
    name_plural = "Roles"
    icon = "fa-solid fa-user-tag"
    category = "Authentication"

    column_list = Role.get_fields()


class UserAdmin(ModelView, model=User):
    name = "User"
    name_plural = "Users"
    icon = "fa-solid fa-users"
    category = "Authentication"

    column_list = User.get_fields()


AUTH_ADMIN_VIEWS = (
    OrganizationAdmin,
    DepartmentAdmin,
    ProfileAdmin,
    RoleAdmin,
    UserAdmin,
)


def register_auth_admin_views(admin: Admin) -> None:
    """Register the core authentication models with SQLAdmin."""
    for admin_view in AUTH_ADMIN_VIEWS:
        admin.add_view(admin_view)