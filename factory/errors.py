class FactoryError(Exception):
    """A factory job stopped because of a rule or a bad request."""


class GovernanceError(FactoryError):
    """Policy refused a command, a path, or a publish."""


class RecipeError(FactoryError):
    """The builder has no safe change for this plan."""


class PlanScopeError(FactoryError):
    """The Builder proposed an in-repository edit outside the approved plan."""
