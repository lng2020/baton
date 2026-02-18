#!/bin/bash
# Merge completed worktree branches back to main

set -e

WORKTREE_BASE="./worktrees"

if [ -z "$1" ]; then
    echo "Usage: $0 <task-id|all>"
    exit 1
fi

merge_task() {
    local task_id=$1
    local branch="task/$task_id"

    # Check if branch exists
    if ! git rev-parse --verify "$branch" >/dev/null 2>&1; then
        echo "Branch $branch does not exist, skipping"
        return 1
    fi

    echo "Merging $branch into main..."
    git checkout main
    git merge "$branch" --no-ff -m "merge: $branch"
    echo "Merged $branch -> main"
}

if [ "$1" = "all" ]; then
    # Merge all task branches
    for dir in "$WORKTREE_BASE"/*/; do
        if [ -d "$dir" ]; then
            task_id=$(basename "$dir")
            merge_task "$task_id" || true
        fi
    done
    echo "All worktree branches merged"
else
    merge_task "$1"
fi
