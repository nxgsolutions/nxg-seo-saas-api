"""Domain-level exceptions raised by services."""


class EmailAlreadyExistsError(Exception):
    """Raised when attempting to register an email that already exists."""

    def __init__(self, email: str) -> None:
        self.email = email
        super().__init__(f"Email already registered: {email}")


class UserNotFoundError(Exception):
    """Raised when no user exists for a given email."""


class AlreadyMemberError(Exception):
    """Raised when a user is already a member of the workspace."""


class CannotRemoveOwnerError(Exception):
    """Raised when attempting to remove a workspace's owner."""


class CannotChangeOwnerRoleError(Exception):
    """Raised when attempting to change the workspace owner's role."""
