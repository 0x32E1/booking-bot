from aiogram.filters import Filter


class RoleFilter(Filter):
    """Passes if the user has any of the given roles ('owner', 'barber')."""

    def __init__(self, *roles: str) -> None:
        self.roles = set(roles)

    async def __call__(self, event, roles: set[str] | None = None) -> bool:
        return bool(roles) and bool(self.roles & roles)
