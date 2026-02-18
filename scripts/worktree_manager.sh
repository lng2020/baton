#!/bin/bash
# Manage Git worktree lifecycle

ACTION=$1
TASK_ID=$2
WORKTREE_BASE="./worktrees"

case $ACTION in
  create)
    git worktree add -b "task/$TASK_ID" "$WORKTREE_BASE/$TASK_ID" main
    # Copy CLAUDE.md and PROGRESS.md to worktree
    cp CLAUDE.md "$WORKTREE_BASE/$TASK_ID/"
    cp PROGRESS.md "$WORKTREE_BASE/$TASK_ID/"
    echo "Worktree created: $WORKTREE_BASE/$TASK_ID"
    ;;
  merge)
    git checkout main
    git merge "task/$TASK_ID" --no-ff -m "merge: task/$TASK_ID"
    echo "Merged task/$TASK_ID -> main"
    ;;
  cleanup)
    git worktree remove "$WORKTREE_BASE/$TASK_ID" --force 2>/dev/null
    git branch -D "task/$TASK_ID" 2>/dev/null
    echo "Cleaned up worktree: $TASK_ID"
    ;;
  list)
    git worktree list
    ;;
  cleanup-all)
    for dir in "$WORKTREE_BASE"/*/; do
      task_id=$(basename "$dir")
      git worktree remove "$dir" --force 2>/dev/null
      git branch -D "task/$task_id" 2>/dev/null
    done
    echo "All worktrees cleaned up"
    ;;
  *)
    echo "Usage: $0 {create|merge|cleanup|list|cleanup-all} [TASK_ID]"
    exit 1
    ;;
esac
