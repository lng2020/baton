# Fix chat session/project mismatch on project switch

In `frontend/js/app.js`, fix the bug where switching projects while a chat stream is in-flight causes the session state from the old project to leak into the new project context.

**Problem**: When switching projects, `selectProject()` calls `resetChat()` which clears `chatSessionId`. But if a streaming response is still running, the `done` event handler (line 393) overwrites `chatSessionId` with the old project's session after the reset. Subsequent messages or task creation then use the wrong project context.

**Fix**:
1. In `sendMessage()` (around line 339), capture `selectedProjectId` at the start of the function into a local variable (e.g., `const targetProjectId = selectedProjectId`). Use this captured value for the fetch URL (line 361) and for all state updates in the response handler.

2. In the streaming response handler, before updating `chatSessionId` from the `done` event (line 393), check that `selectedProjectId` still matches `targetProjectId`. If it doesn't, discard the response — the user has switched projects and this response is stale.

3. Similarly, guard the `chatHistory.push()` at line 402 — only push to history if the project hasn't changed.

4. In `selectProject()` (line 102), when `isStreaming` is true and the user switches projects, set a flag or simply let the guards in sendMessage handle the stale response. Also explicitly set `isStreaming = false` and `btnSend.disabled = false` in `resetChat()` so the user can immediately start chatting with the new project.

5. In `confirmPlan()` (line 453), as an extra safety measure, store the project ID that was active when the plan was generated (e.g., `currentPlanProjectId`) and use that instead of `selectedProjectId` when creating tasks. This ensures tasks go to the correct project even if the user switches projects between seeing the plan and clicking confirm. Alternatively, warn the user if the project has changed since the plan was generated.

**Files to modify**: `frontend/js/app.js`

**Testing**: 
- Open two projects in the dashboard
- Start a chat conversation with Project A
- While streaming, switch to Project B
- Verify the stale response from A doesn't pollute B's state
- Verify sending a message in B goes to B, not A
- Verify plan confirmation sends tasks to the correct project