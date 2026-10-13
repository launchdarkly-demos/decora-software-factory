"""Intake: a feature request from a JSON spec or a GitHub issue body."""

from factory.intake.load import load_feature, load_github_issue, parse_issue_body

__all__ = ["load_feature", "load_github_issue", "parse_issue_body"]
