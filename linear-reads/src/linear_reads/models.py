"""Pydantic models for the slices of Linear's GraphQL schema this tool reads.

Every field is optional because the selection set is driven by ``--fields``:
a node only carries what the query asked for.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Node(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class TeamRef(_Node):
    key: str | None = None
    name: str | None = None


class UserRef(_Node):
    display_name: str | None = None
    name: str | None = None
    active: bool | None = None


class WorkflowState(_Node):
    name: str | None = None
    type: str | None = None
    team: TeamRef | None = None


class Label(_Node):
    name: str | None = None
    team: TeamRef | None = None


class LabelConnection(_Node):
    nodes: list[Label] = Field(default_factory=list)


class ProjectRef(_Node):
    name: str | None = None
    state: str | None = None


class PageInfo(_Node):
    has_next_page: bool = False
    end_cursor: str | None = None


class Issue(_Node):
    identifier: str | None = None
    id: str | None = None
    title: str | None = None
    description: str | None = None
    state: WorkflowState | None = None
    assignee: UserRef | None = None
    labels: LabelConnection | None = None
    project: ProjectRef | None = None
    team: TeamRef | None = None
    priority_label: str | None = None
    estimate: float | None = None
    created_at: str | None = None
    updated_at: str | None = None
    url: str | None = None

    def flat(self, fields: Sequence[str]) -> dict[str, Any]:
        """Project this issue onto the requested field names, one flat value each."""
        getters: dict[str, Callable[[], Any]] = {
            "id": lambda: self.identifier,
            "uuid": lambda: self.id,
            "title": lambda: self.title,
            "state": lambda: self.state.name if self.state else None,
            "assignee": lambda: self.assignee.display_name if self.assignee else None,
            "labels": lambda: [label.name for label in self.labels.nodes] if self.labels else [],
            "project": lambda: self.project.name if self.project else None,
            "team": lambda: self.team.key if self.team else None,
            "priority": lambda: self.priority_label,
            "estimate": lambda: self.estimate,
            "created": lambda: self.created_at,
            "updated": lambda: self.updated_at,
            "url": lambda: self.url,
            "body": lambda: self.description,
        }
        return {name: getters[name]() for name in fields}


class Comment(_Node):
    body: str | None = None
    created_at: str | None = None
    user: UserRef | None = None

    def flat(self) -> dict[str, Any]:
        return {
            "created": self.created_at,
            "author": self.user.display_name if self.user else None,
            "body": self.body,
        }
