from steps.deploy_steps import DEPLOY_STEPS
from steps.env_steps import ENV_STEPS
from steps.test_steps import TEST_STEPS
from steps.base import TestStep


def all_steps() -> list[TestStep]:
    return [*DEPLOY_STEPS, *ENV_STEPS, *TEST_STEPS]


def steps_by_category(category: str) -> list[TestStep]:
    return [s for s in all_steps() if s.category == category]
