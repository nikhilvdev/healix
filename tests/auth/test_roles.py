"""Role names, and where a role's credentials live."""

import pytest

from healix.auth.roles import (
    ANONYMOUS,
    RoleError,
    credential_env_names,
    env_suffix,
    validate_roles,
)


def test_a_role_is_named_by_capitals_in_the_environment():
    assert credential_env_names("admin") == (
        "WEBLIB_LOGIN_USERNAME_ADMIN",
        "WEBLIB_LOGIN_PASSWORD_ADMIN",
    )
    assert env_suffix("read-only") == "READ_ONLY"
    assert credential_env_names("read_only")[0] == "WEBLIB_LOGIN_USERNAME_READ_ONLY"


def test_good_names_are_kept_in_order():
    assert validate_roles(["admin", "standard", ANONYMOUS, "read-only", "team_2"]) == (
        "admin",
        "standard",
        "anonymous",
        "read-only",
        "team_2",
    )


@pytest.mark.parametrize(
    "bad",
    ["Admin", "1st", "-x", "has space", "a/b", "../etc", "", "x" * 33, "ünï", "roles", "pages"],
)
def test_names_that_cannot_be_a_folder_and_a_variable_are_refused(bad):
    with pytest.raises(RoleError):
        validate_roles(["ok", bad])


@pytest.mark.parametrize("bad", [[], "admin", None, 5, [1], [None]])
def test_it_must_be_a_non_empty_list_of_strings(bad):
    with pytest.raises(RoleError):
        validate_roles(bad)


def test_the_same_name_twice_is_refused():
    with pytest.raises(RoleError, match="listed twice"):
        validate_roles(["admin", "admin"])


def test_two_names_that_share_an_environment_variable_are_refused():
    with pytest.raises(RoleError, match="share WEBLIB_LOGIN_USERNAME_A_B|share A_B"):
        validate_roles(["a-b", "a_b"])
