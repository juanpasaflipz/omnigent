"""The one-click "software factory" template (``POST /v1/templates/factory``).

Installs the ``examples/factory`` foreman as the caller's own user agent, with
their budget and merge gate written into its policies, connects their GitHub
repository's webhook (:mod:`omnigent.server.github_webhooks`) and creates the
launch subscriptions that start a foreman session per new issue and per
failed check. Calling it again for the same user and repository returns the
same agent and subscriptions instead of duplicating them.
"""

from __future__ import annotations

import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from omnigent._platform import resolve_repo_symlink
from omnigent.entities import Subscription, SubscriptionState
from omnigent.resources import examples as _examples_resources
from omnigent.spec import materialize_bundle

# Packaged under omnigent.resources.examples (a symlink in a checkout, a real
# directory in a wheel), like the seeded polly and debby bundles.
FACTORY_BUNDLE_SOURCE = resolve_repo_symlink(Path(_examples_resources.__file__).parent / "factory")

DEFAULT_DAILY_BUDGET_USD = 25.0
MAX_DAILY_BUDGET_USD = 1000.0
# The foreman asks to continue once spend crosses this share of the budget.
_ASK_THRESHOLD_SHARE = 0.4

_COST_BUDGET_PATH = "omnigent.policies.builtins.cost.cost_budget"
_BLAST_RADIUS_PATH = "omnigent.policies.builtins.orchestration.blast_radius"

# Launched sessions already run off these subscriptions; the foreman must not add more.
_NO_RESUBSCRIBE = (
    "Subscriptions for new issues and failed checks already exist; do not add them again.\n"
)
_ISSUE_PROMPT = (
    "A new issue was opened in {repo}. Triage it: dispatch the triager with the issue "
    "number and title, then follow your process for its report.\n"
    + _NO_RESUBSCRIBE
    + "Issue event: {{fields_json}}"
)
_FAILED_CHECK_PROMPT = (
    "A check failed in {repo}. If it is on a pull request your fixer opened, dispatch "
    "the fixer on that pull request with the check name and URL; otherwise post one "
    "line and stop.\n" + _NO_RESUBSCRIBE + "Check event: {{fields_json}}"
)


@dataclass(frozen=True)
class FactorySubscriptionSpec:
    """One launch subscription the template creates."""

    kind: str
    match: dict[str, Any]
    note: str
    prompt_template: str


@dataclass
class FactoryPlan:
    """What the template installs for one repository."""

    owner: str
    repo: str
    budget_usd: float
    ask_before_merge: bool
    subscriptions: list[FactorySubscriptionSpec] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def agent_name(self) -> str:
        return factory_agent_name(self.owner, self.repo)


def factory_agent_name(owner: str, repo: str) -> str:
    """The foreman's agent name for one repository, e.g. ``factory-acme-api``.

    Agent names allow only letters, digits, ``-`` and ``_``, so dots and other
    characters in GitHub names collapse to ``-``.
    """
    return re.sub(r"[^a-z0-9_-]+", "-", f"factory-{owner}-{repo}".lower()).strip("-")


def factory_plan(
    owner: str, repo: str, *, budget_usd: float, ask_before_merge: bool
) -> FactoryPlan:
    """Build the plan (agent settings plus subscriptions) for ``owner/repo``."""
    full_name = f"{owner}/{repo}"
    return FactoryPlan(
        owner=owner,
        repo=repo,
        budget_usd=budget_usd,
        ask_before_merge=ask_before_merge,
        subscriptions=[
            FactorySubscriptionSpec(
                kind="issues.opened",
                match={"repo": full_name},
                note="factory: triage new issues",
                prompt_template=_ISSUE_PROMPT.format(repo=full_name),
            ),
            FactorySubscriptionSpec(
                kind="check_run.completed",
                match={"repo": full_name, "conclusion": "failure"},
                note="factory: fix failed checks",
                prompt_template=_FAILED_CHECK_PROMPT.format(repo=full_name),
            ),
        ],
    )


def _function_policy(config: dict[str, Any], path: str) -> dict[str, Any]:
    """The foreman's guardrail policy whose function is *path*."""
    policies = (config.get("guardrails") or {}).get("policies") or {}
    for policy in policies.values():
        function = policy.get("function") if isinstance(policy, dict) else None
        if isinstance(function, dict) and function.get("path") == path:
            return function
    raise RuntimeError(f"factory bundle has no {path} policy")


def apply_factory_settings(config: dict[str, Any], plan: FactoryPlan) -> dict[str, Any]:
    """Write the plan's name, budget and merge gate into a foreman ``config.yaml``.

    :param config: The parsed foreman ``config.yaml``; modified in place.
    :returns: *config*.
    """
    config["name"] = plan.agent_name
    budget = _function_policy(config, _COST_BUDGET_PATH)
    threshold = round(plan.budget_usd * _ASK_THRESHOLD_SHARE, 2)
    budget["arguments"] = {
        "max_cost_usd": plan.budget_usd,
        # cost_budget requires every checkpoint to sit strictly below the cap.
        "ask_thresholds_usd": [threshold] if 0 < threshold < plan.budget_usd else [],
    }
    blast_radius = _function_policy(config, _BLAST_RADIUS_PATH)
    blast_radius["arguments"] = {
        **(blast_radius.get("arguments") or {}),
        "gate_pushes": plan.ask_before_merge,
    }
    return config


def build_factory_bundle(plan: FactoryPlan) -> bytes:
    """Tar the packaged factory bundle with the plan applied to its foreman.

    :returns: A deterministic gzipped tarball, ready for agent install.
    """
    # Shares the deterministic tar used for edited agent bundles.
    from omnigent.server.routes.session_mcp_servers import _tar_gz_dir

    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = materialize_bundle(FACTORY_BUNDLE_SOURCE, Path(tmpdir) / "bundle")
        config_path = bundle_dir / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        apply_factory_settings(config, plan)
        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        return _tar_gz_dir(bundle_dir)


def ensure_factory_subscriptions(
    store: Any,
    plan: FactoryPlan,
    *,
    owner_id: str | None,
    agent_id: str,
    execution_target: str,
) -> list[tuple[Subscription, bool]]:
    """Create the plan's launch subscriptions, reusing any the caller already has.

    A subscription is reused when it targets the same agent with the same
    source, kind and filter and is not deleted (a paused one stays paused).

    :returns: ``(subscription, created)`` for each planned subscription.
    """
    existing = [
        subscription
        for subscription in store.list_for_user(owner_id)
        if subscription.state != SubscriptionState.DELETED.value
        and subscription.agent_id == agent_id
        and subscription.source == "github"
    ]
    results: list[tuple[Subscription, bool]] = []
    for spec in plan.subscriptions:
        found = next(
            (
                subscription
                for subscription in existing
                if subscription.kind == spec.kind and subscription.match == spec.match
            ),
            None,
        )
        if found is not None:
            results.append((found, False))
            continue
        created = store.create(
            uuid.uuid4().hex,
            user_id=owner_id,
            agent_id=agent_id,
            source="github",
            kind=spec.kind,
            match=spec.match,
            note=spec.note,
            prompt_template=spec.prompt_template,
            execution_target=execution_target,
        )
        results.append((created, True))
    return results
