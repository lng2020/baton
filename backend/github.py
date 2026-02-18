from __future__ import annotations

import json
import subprocess

from backend.models import PRInfo


def get_task_branch_name(task_id: str) -> str:
    return f"task/{task_id}"


def get_pr_for_branch(repo: str, branch: str) -> PRInfo | None:
    if not repo:
        return None
    try:
        result = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", repo,
                "--head", branch,
                "--json", "number,title,url,state,headRefName",
                "--limit", "1",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not prs:
        return None
    pr = prs[0]
    return PRInfo(
        number=pr["number"],
        title=pr["title"],
        url=pr["url"],
        state=pr["state"],
        branch=pr["headRefName"],
    )
