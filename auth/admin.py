from sqladmin import ModelView


from app.damapql.auth.models import Organization, Department, Profile, Role, User
from main import dch_admin


class OrganizationAdmin(ModelView, model=Organization):
    name = "Organization"
    name_plural = "Organizations"
    icon = "fa-solid fa-building"
    column_list = Organization.get_fields()


class DepartmentAdmin(ModelView, model=Department):
    name = "Department"
    name_plural = "Departments"
    icon = "fa-solid fa-sitemap"
    column_list = Department.get_fields()


class ProfileAdmin(ModelView, model=Profile):
    name = "Profile"
    name_plural = "Profiles"
    icon = "fa-solid fa-id-card"
    column_list = Profile.get_fields()


class RoleAdmin(ModelView, model=Role):
    name = "Role"
    name_plural = "Roles"
    icon = "fa-solid fa-user-tag"
    column_list = Role.get_fields()


class UserAdmin(ModelView, model=User):
    name = "User"
    name_plural = "Users"
    icon = "fa-solid fa-users"
    column_list = User.get_fields()


dch_admin.add_view(OrganizationAdmin)
dch_admin.add_view(DepartmentAdmin)
dch_admin.add_view(ProfileAdmin)
dch_admin.add_view(RoleAdmin)
dch_admin.add_view(UserAdmin)