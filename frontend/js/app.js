// Baton — Single-layout dashboard controller
(function () {
    // ---- State ----
    let selectedProjectId = null;
    let projects = [];
    const statuses = ["pending", "plan_review", "in_progress", "completed", "failed"];

    // Polling cache — skip re-render when data unchanged
    let lastTasksJson = null;
    let lastWorktreesJson = null;
    let lastCommitsJson = null;

    // Chat state
    let chatHistory = [];
    let chatSessionId = null;
    let currentPlan = null;
    let currentPlanProjectId = null;
    let isStreaming = false;
    let chatMode = 'task';

    // Per-project chat state store — preserves conversation when switching projects
    const projectChatStates = {};

    // ---- DOM refs ----
    const projectList = document.getElementById("project-list");
    const kanbanTitle = document.getElementById("kanban-title");
    const projectHealth = document.getElementById("project-health");
    const worktreesContent = document.getElementById("worktrees-content");
    const commitsContent = document.getElementById("commits-content");
    // Detail panel refs
    const detailOverlay = document.getElementById("detail-overlay");
    const detailPanel = document.getElementById("detail-panel");
    const detailBody = document.getElementById("detail-body");
    const detailClose = document.getElementById("detail-close");

    // Sidebar toggle refs
    const toggleLeft = document.getElementById("toggle-left");
    const toggleRight = document.getElementById("toggle-right");
    const sidebarLeft = document.getElementById("sidebar-left");
    const sidebarRight = document.getElementById("sidebar-right");

    // Chat refs (inline)
    const chatSection = document.getElementById("chat-section");
    const chatBody = document.getElementById("chat-body");
    const chatMessages = document.getElementById("chat-messages");
    const chatInput = document.getElementById("chat-input");
    const btnSend = document.getElementById("btn-send");
    const btnChatToggle = document.getElementById("btn-chat-toggle");
    const btnChatClear = document.getElementById("btn-chat-clear");
    const chatPlanEl = document.getElementById("chat-plan");
    const chatPlanTasks = document.getElementById("chat-plan-tasks");
    const chatPlanSummary = document.getElementById("chat-plan-summary");

    const taskForm = document.getElementById('task-form');
    const taskTitle = document.getElementById('task-title');
    const taskType = document.getElementById('task-type');
    const taskContent = document.getElementById('task-content');
    const btnTaskSubmit = document.getElementById('btn-task-submit');
    const modeToggleBtns = document.querySelectorAll('.mode-btn');
    const chatHeaderTitle = document.querySelector('.chat-header h3');

    // Image upload refs
    const imageInput = document.getElementById('image-input');
    const btnUpload = document.getElementById('btn-upload');
    const imagePreviews = document.getElementById('image-previews');

    const GREETING = "Hi! I'm your agent engineer. Describe what you'd like to accomplish, and I'll help you plan the tasks.";

    function switchMode(mode) {
        chatMode = mode;
        modeToggleBtns.forEach(btn => {
            btn.classList.toggle('active', btn.dataset.mode === mode);
        });
        if (mode === 'plan') {
            chatBody.style.display = '';
            taskForm.style.display = 'none';
            chatHeaderTitle.textContent = 'Plan Mode';
            btnChatClear.style.display = '';
        } else {
            chatBody.style.display = 'none';
            taskForm.style.display = '';
            chatHeaderTitle.textContent = 'New Task';
            btnChatClear.style.display = 'none';
        }
    }

    modeToggleBtns.forEach(btn => {
        btn.addEventListener('click', () => switchMode(btn.dataset.mode));
    });

    async function submitTask() {
        const title = taskTitle.value.trim();
        const content = taskContent.value.trim();
        const task_type = taskType.value;
        if (!title || !content || !selectedProjectId) return;
        const targetProjectId = selectedProjectId;
        const needs_plan_review = chatMode === 'plan';
        try {
            const res = await fetch(`/api/projects/${targetProjectId}/tasks`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title, content, task_type, needs_plan_review }),
            });
            if (!res.ok) throw new Error(res.statusText);
            taskTitle.value = '';
            taskContent.value = '';
            taskType.value = 'feature';
            imagePreviews.innerHTML = '';
            if (selectedProjectId === targetProjectId) {
                loadTasks();
            }
        } catch (err) {
            alert('Failed to create task: ' + err.message);
        }
    }

    // ---- Image Upload ----
    btnUpload.addEventListener('click', () => imageInput.click());

    imageInput.addEventListener('change', async () => {
        const files = Array.from(imageInput.files || []);
        imageInput.value = '';
        if (!files.length || !selectedProjectId) return;

        for (const file of files) {
            const previewId = 'img-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6);
            const item = document.createElement('div');
            item.className = 'image-preview-item uploading';
            item.id = previewId;

            const img = document.createElement('img');
            img.src = URL.createObjectURL(file);
            item.appendChild(img);
            imagePreviews.appendChild(item);

            try {
                const form = new FormData();
                form.append('file', file);
                const res = await fetch(`/api/projects/${selectedProjectId}/upload`, {
                    method: 'POST',
                    body: form,
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({ detail: res.statusText }));
                    throw new Error(err.detail || res.statusText);
                }
                const data = await res.json();
                item.classList.remove('uploading');

                // Add remove button
                const removeBtn = document.createElement('button');
                removeBtn.className = 'image-preview-remove';
                removeBtn.textContent = '\u00d7';
                removeBtn.addEventListener('click', () => {
                    item.remove();
                    // Remove the markdown reference from content
                    const pattern = `![${data.original_name}](${data.url})`;
                    taskContent.value = taskContent.value.replace(pattern + '\n', '').replace(pattern, '');
                });
                item.appendChild(removeBtn);

                // Insert markdown image link into the textarea
                const imageRef = `![${data.original_name}](${data.url})`;
                if (taskContent.value && !taskContent.value.endsWith('\n')) {
                    taskContent.value += '\n';
                }
                taskContent.value += imageRef + '\n';
            } catch (err) {
                item.remove();
                alert('Image upload failed: ' + err.message);
            }
        }
    });

    // ---- Helpers ----
    function escHtml(s) {
        const d = document.createElement("div");
        d.textContent = s || "";
        return d.innerHTML;
    }

    function escAttr(s) {
        return (s || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/'/g, "&#39;");
    }

    // ---- Projects ----
    async function loadProjects() {
        try {
            const res = await fetch("/api/projects");
            if (!res.ok) throw new Error(res.statusText);
            projects = await res.json();
            renderProjectList();
            // Auto-select first project if none selected
            if (!selectedProjectId && projects.length) {
                selectProject(projects[0].id);
            }
        } catch (err) {
            projectList.innerHTML = '<div class="empty-state">Failed to load projects</div>';
            console.error(err);
        }
    }

    function renderProjectList() {
        if (!projects.length) {
            projectList.innerHTML = '<div class="empty-state">No projects</div>';
            return;
        }
        projectList.innerHTML = projects.map(p => {
            const c = p.task_counts;
            const selected = p.id === selectedProjectId ? " selected" : "";
            const healthClass = p.healthy ? "healthy" : "unhealthy";
            const counts = statuses
                .filter(s => (c[s] || 0) > 0)
                .map(s => `<span class="project-item-count ${s}">${c[s]}</span>`)
                .join("");
            return `
                <div class="project-item${selected}" data-id="${escAttr(p.id)}" onclick="window._selectProject('${escAttr(p.id)}')">
                    <span class="health-dot ${healthClass}"></span>
                    <span class="project-item-name">${escHtml(p.name)}</span>
                    <span class="project-item-counts">${counts}</span>
                </div>
            `;
        }).join("");
    }

    function selectProject(id) {
        const previousProjectId = selectedProjectId;

        // Save chat state for the project we're leaving
        if (previousProjectId && previousProjectId !== id) {
            saveChatState(previousProjectId);
        }

        selectedProjectId = id;
        const proj = projects.find(p => p.id === id);

        // Update header
        kanbanTitle.textContent = proj ? proj.name : "Select a project";
        if (proj) {
            projectHealth.className = "health-dot " + (proj.healthy ? "healthy" : "unhealthy");
            projectHealth.style.display = "";
        } else {
            projectHealth.style.display = "none";
        }

        // Show chat section and restore (or init) conversation for this project
        if (proj) {
            chatSection.style.display = "";
            if (previousProjectId !== id) {
                restoreChatState(id);
            }
        } else {
            chatSection.style.display = "none";
        }

        // Highlight in sidebar
        renderProjectList();

        // Reset polling caches so project switch always renders
        lastTasksJson = null;
        lastWorktreesJson = null;
        lastCommitsJson = null;

        // Load data
        loadTasks();
        loadWorktrees();
        loadCommits();
    }

    // Expose to onclick handlers
    window._selectProject = selectProject;

    // ---- Kanban Board ----
    async function loadTasks() {
        if (!selectedProjectId) return;
        const targetProjectId = selectedProjectId;
        try {
            const res = await fetch(`/api/projects/${targetProjectId}/tasks`);
            if (!res.ok) throw new Error(res.statusText);
            const tasks = await res.json();
            if (selectedProjectId !== targetProjectId) return;
            const json = JSON.stringify(tasks);
            if (json === lastTasksJson) return;
            lastTasksJson = json;
            renderTasks(tasks);
        } catch (err) {
            console.error("Failed to load tasks:", err);
        }
    }

    function renderTasks(tasks) {
        for (const status of statuses) {
            const list = document.querySelector(`[data-list="${status}"]`);
            const count = document.querySelector(`[data-count="${status}"]`);
            const items = tasks[status] || [];
            count.textContent = items.length;
            if (!items.length) {
                list.innerHTML = '<div class="empty-state">No tasks</div>';
                continue;
            }
            list.innerHTML = items.map(t => {
                const modified = new Date(t.modified).toLocaleDateString();
                const errorBadge = t.has_error_log ? '<span class="error-badge">error log</span>' : "";
                const typeBadge = t.task_type ? `<span class="task-type-badge ${escAttr(t.task_type)}">${escHtml(t.task_type)}</span>` : "";
                const planBadge = t.has_plan ? '<span class="plan-badge">plan</span>' : "";
                return `
                    <div class="task-card" onclick="window._openTaskDetail('${status}', '${escAttr(t.filename)}')">
                        <h4>${typeBadge}${escHtml(t.title)}${planBadge}</h4>
                        <div class="task-meta">${escHtml(t.id)} &middot; ${modified}</div>
                        ${errorBadge}
                    </div>
                `;
            }).join("");
        }
    }

    // ---- Task Detail Panel ----
    function openTaskDetail(status, filename) {
        const targetProjectId = selectedProjectId;
        detailBody.innerHTML = '<div class="loading">Loading...</div>';
        detailOverlay.classList.add("open");
        detailPanel.classList.add("open");

        fetch(`/api/projects/${targetProjectId}/tasks/${status}/${filename}`)
            .then(res => {
                if (!res.ok) throw new Error(res.statusText);
                return res.json();
            })
            .then(task => {
                if (selectedProjectId !== targetProjectId) return;
                renderDetail(task);
            })
            .catch(err => {
                if (selectedProjectId !== targetProjectId) return;
                detailBody.innerHTML = `<div class="empty-state">Failed to load task: ${escHtml(err.message)}</div>`;
            });
    }

    function closeDetail() {
        detailOverlay.classList.remove("open");
        detailPanel.classList.remove("open");
    }

    function renderDetail(task) {
        const typeBadge = task.task_type ? `<span class="task-type-badge ${escAttr(task.task_type)}">${escHtml(task.task_type)}</span>` : "";
        let html = `
            <h2>${escHtml(task.title)}</h2>
            <span class="detail-status ${task.status}">${task.status.replace("_", " ")}</span>
            ${typeBadge}
            <div style="font-size:0.8rem;color:var(--text-muted);margin-top:0.5rem;">
                ${escHtml(task.id)} &middot; Modified: ${new Date(task.modified).toLocaleString()}
            </div>
        `;

        if (task.pr) {
            html += `
                <div class="detail-section">
                    <h3>Pull Request</h3>
                    <div class="detail-pr">
                        <a href="${escAttr(task.pr.url)}" target="_blank">#${task.pr.number} ${escHtml(task.pr.title)}</a>
                        <div style="font-size:0.8rem;color:var(--text-muted);margin-top:0.3rem;">
                            ${escHtml(task.pr.state)} &middot; ${escHtml(task.pr.branch)}
                        </div>
                    </div>
                </div>
            `;
        }

        html += `
            <div class="detail-section">
                <h3>Content</h3>
                <div class="detail-content">${escHtml(task.content)}</div>
            </div>
        `;

        if (task.plan_content) {
            html += `
                <div class="detail-section">
                    <h3>Implementation Plan</h3>
                    <div class="detail-content">${escHtml(task.plan_content)}</div>
                </div>
            `;
        }

        if (task.status === "plan_review") {
            html += `
                <div class="detail-section">
                    <h3>Review Actions</h3>
                    <div class="plan-review-actions">
                        <button class="btn-approve" onclick="window._approvePlan('${escAttr(task.id)}')">Approve</button>
                        <button class="btn-revise" onclick="window._showReviseDialog('${escAttr(task.id)}')">Revise</button>
                        <button class="btn-reject" onclick="window._rejectPlan('${escAttr(task.id)}')">Reject</button>
                    </div>
                    <div id="revise-dialog" style="display:none;margin-top:0.75rem;">
                        <textarea class="revise-feedback" id="revise-feedback" rows="3" placeholder="What should be changed in the plan?"></textarea>
                        <div class="plan-review-actions" style="margin-top:0.5rem;">
                            <button class="btn-revise" onclick="window._revisePlan('${escAttr(task.id)}')">Submit Revision</button>
                            <button class="btn-cancel" onclick="document.getElementById('revise-dialog').style.display='none'">Cancel</button>
                        </div>
                    </div>
                </div>
            `;
        }

        if (task.error_log) {
            html += `
                <div class="detail-section">
                    <h3>Error Log</h3>
                    <div class="detail-content detail-error-log">${escHtml(task.error_log)}</div>
                </div>
            `;
        }

        detailBody.innerHTML = html;
    }

    // Expose to onclick handlers
    window._openTaskDetail = openTaskDetail;

    detailClose.addEventListener("click", closeDetail);
    detailOverlay.addEventListener("click", closeDetail);
    document.addEventListener("keydown", e => {
        if (e.key === "Escape") {
            closeDetail();
        }
    });

    // ---- Worktrees ----
    async function loadWorktrees() {
        if (!selectedProjectId) return;
        const targetProjectId = selectedProjectId;
        try {
            const res = await fetch(`/api/projects/${targetProjectId}/worktrees`);
            if (!res.ok) return;
            const worktrees = await res.json();
            if (selectedProjectId !== targetProjectId) return;
            const json = JSON.stringify(worktrees);
            if (json === lastWorktreesJson) return;
            lastWorktreesJson = json;
            if (!worktrees.length) {
                worktreesContent.innerHTML = '<div class="empty-state">No worktrees</div>';
                return;
            }
            worktreesContent.innerHTML = worktrees.map(w => `
                <div class="info-item">
                    <strong>${escHtml(w.branch || "(detached)")}</strong><br>
                    <code>${escHtml(w.commit ? w.commit.substring(0, 8) : "")}</code>
                    &middot; ${escHtml(w.path)}
                    ${w.is_bare ? " (bare)" : ""}
                </div>
            `).join("");
        } catch (err) {
            console.error("Failed to load worktrees:", err);
        }
    }

    // ---- Recent Commits ----
    async function loadCommits() {
        if (!selectedProjectId) return;
        const targetProjectId = selectedProjectId;
        try {
            const res = await fetch(`/api/projects/${targetProjectId}/commits`);
            if (!res.ok) return;
            const commits = await res.json();
            if (selectedProjectId !== targetProjectId) return;
            const json = JSON.stringify(commits);
            if (json === lastCommitsJson) return;
            lastCommitsJson = json;
            if (!commits.length) {
                commitsContent.innerHTML = '<div class="empty-state">No commits</div>';
                return;
            }
            commitsContent.innerHTML = commits.map(c => `
                <div class="info-item">
                    <code>${escHtml(c.sha.substring(0, 8))}</code>
                    <strong>${escHtml(c.message)}</strong><br>
                    ${escHtml(c.author)} &middot; ${escHtml(c.date)}
                </div>
            `).join("");
        } catch (err) {
            console.error("Failed to load commits:", err);
        }
    }

    // ---- Inline Chat ----
    function saveChatState(projectId) {
        if (!projectId) return;
        projectChatStates[projectId] = {
            history: chatHistory.slice(),
            sessionId: chatSessionId,
            plan: currentPlan,
            planProjectId: currentPlanProjectId,
            messagesHtml: chatMessages.innerHTML,
            inputValue: chatInput.value,
            planVisible: chatPlanEl.style.display !== "none",
            collapsed: chatSection.classList.contains("collapsed"),
            mode: chatMode,
        };
    }

    function restoreChatState(projectId) {
        const state = projectChatStates[projectId];
        if (!state) {
            resetChat();
            return;
        }
        chatHistory = state.history.slice();
        chatSessionId = state.sessionId;
        currentPlan = state.plan;
        currentPlanProjectId = state.planProjectId;
        isStreaming = false;
        btnSend.disabled = false;
        chatMessages.innerHTML = state.messagesHtml;
        chatInput.value = state.inputValue;
        chatPlanEl.style.display = state.planVisible ? "block" : "none";
        if (state.collapsed) {
            chatSection.classList.add("collapsed");
        } else {
            chatSection.classList.remove("collapsed");
        }
        switchMode(state.mode);
    }

    function resetChat() {
        chatHistory = [];
        chatSessionId = null;
        currentPlan = null;
        currentPlanProjectId = null;
        isStreaming = false;
        btnSend.disabled = false;
        chatMessages.innerHTML = `<div class="chat-message assistant"><div class="chat-bubble">${escHtml(GREETING)}</div></div>`;
        chatPlanEl.style.display = "none";
        chatInput.value = "";
        chatSection.classList.remove("collapsed");
        // Also clear the saved state for the current project
        if (selectedProjectId) {
            delete projectChatStates[selectedProjectId];
        }
    }

    function appendMessage(role, content) {
        const div = document.createElement("div");
        div.className = `chat-message ${role}`;
        div.innerHTML = `<div class="chat-bubble">${escHtml(content)}</div>`;
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function appendStreamingBubble() {
        const div = document.createElement("div");
        div.className = "chat-message assistant";
        div.innerHTML = '<div class="chat-bubble"></div>';
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
        return div.querySelector(".chat-bubble");
    }

    /**
     * Parse assistant response text for questions with numbered/bulleted options
     * and render them as clickable chips. Patterns detected:
     *   - Lines ending with "?" followed by numbered options (1. / 2. / - / *)
     *   - Markdown bold options like **Option A** — description
     */
    function renderPlanBubble(bubble, text) {
        // Split into sections by question marks at end of lines
        const lines = text.split('\n');
        let html = '';
        let i = 0;
        while (i < lines.length) {
            const line = lines[i];
            // Detect a question line (ends with ?)
            if (line.trim().endsWith('?')) {
                html += `<div class="plan-question">${escHtml(line.trim())}</div>`;
                i++;
                // Collect option lines following the question
                const options = [];
                while (i < lines.length) {
                    const optLine = lines[i].trim();
                    // Match: "1. text", "- text", "* text", "a) text", "A. text"
                    const optMatch = optLine.match(/^(?:\d+[.)]\s*|[-*]\s+|[a-zA-Z][.)]\s*)(.+)/);
                    if (optMatch) {
                        options.push(optMatch[1].trim());
                        i++;
                    } else if (optLine === '') {
                        i++;
                        // Check if next line is still an option
                        if (i < lines.length && lines[i].trim().match(/^(?:\d+[.)]\s*|[-*]\s+|[a-zA-Z][.)]\s*)/)) {
                            continue;
                        }
                        break;
                    } else {
                        break;
                    }
                }
                if (options.length > 0) {
                    html += '<div class="plan-options">';
                    for (const opt of options) {
                        html += `<button class="plan-option-btn" onclick="window._selectPlanOption(this)">${escHtml(opt)}</button>`;
                    }
                    html += '</div>';
                }
            } else {
                html += escHtml(line) + '\n';
                i++;
            }
        }
        bubble.innerHTML = html;
    }

    function selectPlanOption(btn) {
        // Highlight the selected option
        const container = btn.parentElement;
        container.querySelectorAll('.plan-option-btn').forEach(b => b.classList.remove('selected'));
        btn.classList.add('selected');
        // Store selections for sending
        if (!window._planSelections) window._planSelections = [];
        window._planSelections.push(btn.textContent);
    }
    window._selectPlanOption = selectPlanOption;

    /**
     * After streaming completes in plan mode, send any selected options
     * as a follow-up message automatically, or show action buttons.
     */
    function finalizePlanResponse(fullResponse) {
        // Find the last assistant bubble and re-render with interactive options
        const bubbles = chatMessages.querySelectorAll('.chat-message.assistant .chat-bubble');
        const lastBubble = bubbles[bubbles.length - 1];
        if (lastBubble) {
            renderPlanBubble(lastBubble, fullResponse);
        }

        // If plan JSON is detected, show confirm/revise actions
        if (tryParsePlan(fullResponse)) return;

        // Show "Send selections" button if there are selectable options
        const hasOptions = lastBubble && lastBubble.querySelectorAll('.plan-option-btn').length > 0;
        if (hasOptions) {
            const actionsDiv = document.createElement('div');
            actionsDiv.className = 'plan-chat-actions';
            actionsDiv.innerHTML = `
                <button class="btn-approve" onclick="window._sendPlanSelections()">Send Selections</button>
            `;
            chatMessages.appendChild(actionsDiv);
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }
    }

    function sendPlanSelections() {
        const selections = window._planSelections || [];
        window._planSelections = [];
        // Remove the action buttons
        chatMessages.querySelectorAll('.plan-chat-actions').forEach(el => el.remove());
        // Collect all selected options
        const selectedBtns = chatMessages.querySelectorAll('.plan-option-btn.selected');
        const answers = [];
        selectedBtns.forEach(btn => answers.push(btn.textContent));
        if (answers.length === 0) return;
        // Send as a user message
        chatInput.value = answers.join('; ');
        sendMessage();
    }
    window._sendPlanSelections = sendPlanSelections;

    async function sendMessage() {
        const text = chatInput.value.trim();
        if (!text || !selectedProjectId || isStreaming) return;

        // Capture project context at call time so a mid-stream project
        // switch doesn't leak state into the wrong project.
        const targetProjectId = selectedProjectId;

        // Expand chat if collapsed
        chatSection.classList.remove("collapsed");

        appendMessage("user", text);
        chatHistory.push({ role: "user", content: text });
        chatInput.value = "";
        isStreaming = true;
        btnSend.disabled = true;

        const bubble = appendStreamingBubble();
        let fullResponse = "";

        try {
            const payload = { messages: chatHistory };
            if (chatSessionId) {
                payload.session_id = chatSessionId;
            }

            const res = await fetch(`/api/projects/${targetProjectId}/chat`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!res.ok) throw new Error(res.statusText);

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split("\n");
                buffer = lines.pop();

                for (const line of lines) {
                    if (!line.startsWith("data: ")) continue;
                    try {
                        const data = JSON.parse(line.slice(6));
                        if (data.type === "text") {
                            fullResponse += data.text;
                            bubble.textContent = fullResponse;
                            chatMessages.scrollTop = chatMessages.scrollHeight;
                        } else if (data.type === "error") {
                            console.error("[chat] SSE error event:", data.error);
                            bubble.textContent = "Error: " + data.error;
                            bubble.style.color = "var(--failed)";
                        } else if (data.type === "done") {
                            console.log("[chat] stream done: session_id=%s, response_length=%d", data.session_id, fullResponse.length);
                            if (fullResponse.length === 0) {
                                console.warn("[chat] stream completed with EMPTY response — plan will show blank content");
                            }
                            // Discard stale response if user switched projects
                            if (selectedProjectId !== targetProjectId) break;
                            if (data.session_id) {
                                chatSessionId = data.session_id;
                            }
                            if (chatMode === 'plan') {
                                finalizePlanResponse(fullResponse);
                            } else {
                                tryParsePlan(fullResponse);
                            }
                        }
                    } catch (_) { /* ignore malformed SSE */ }
                }
            }

            // Only update history if still on the same project
            if (selectedProjectId === targetProjectId) {
                chatHistory.push({ role: "assistant", content: fullResponse });
            }
        } catch (err) {
            bubble.textContent = "Error: " + err.message;
            bubble.style.color = "var(--failed)";
        } finally {
            isStreaming = false;
            btnSend.disabled = false;
        }
    }

    function tryParsePlan(text) {
        console.log("[plan] tryParsePlan called: text_length=%d", text.length);
        if (!text.includes('"plan"') || !text.includes('"tasks"')) {
            console.log("[plan] no plan markers found in response (missing '\"plan\"' or '\"tasks\"')");
            return;
        }

        // Find the opening brace — prefer after ```json marker if present
        let searchFrom = 0;
        const marker = text.indexOf("```json");
        if (marker !== -1) {
            searchFrom = marker + 7;
            console.log("[plan] found ```json marker at position %d", marker);
        }

        const braceStart = text.indexOf("{", searchFrom);
        if (braceStart === -1) {
            console.warn("[plan] plan markers found but no opening brace after position %d", searchFrom);
            return;
        }

        // Try parsing from braceStart to each closing brace, outermost first
        let end = text.lastIndexOf("}");
        let attempts = 0;
        while (end > braceStart) {
            attempts++;
            try {
                const plan = JSON.parse(text.substring(braceStart, end + 1));
                if (plan.plan && plan.tasks && plan.tasks.length) {
                    console.log("[plan] successfully parsed plan: %d tasks, summary=%s", plan.tasks.length, plan.summary);
                    currentPlan = plan;
                    currentPlanProjectId = selectedProjectId;
                    showPlan(plan);
                    return;
                }
                console.log("[plan] parsed JSON but missing plan=true or tasks (plan=%s, tasks=%s)", plan.plan, Array.isArray(plan.tasks) ? plan.tasks.length : "not array");
            } catch (_) { /* try shorter substring */ }
            end = text.lastIndexOf("}", end - 1);
        }
        console.warn("[plan] failed to parse plan JSON after %d attempts", attempts);
    }

    function showPlan(plan) {
        chatPlanSummary.textContent = plan.summary || "";
        chatPlanTasks.innerHTML = plan.tasks.map((t, i) => `
            <div class="plan-task-item">
                <span class="plan-task-num">${i + 1}</span>
                <div class="plan-task-detail">
                    <strong>${escHtml(t.title)}</strong>
                    <div class="plan-task-content">${escHtml(t.content)}</div>
                </div>
            </div>
        `).join("");
        chatPlanEl.style.display = "block";
        chatPlanEl.scrollIntoView({ behavior: "smooth" });
    }

    async function confirmPlan() {
        if (!currentPlan || !currentPlanProjectId) return;
        const targetProjectId = currentPlanProjectId;
        if (targetProjectId !== selectedProjectId) {
            if (!confirm(`This plan was generated for a different project. Create tasks in "${targetProjectId}" anyway?`)) {
                return;
            }
        }
        try {
            // Create each planned task with needs_plan_review=true
            const tasks = currentPlan.tasks || [];
            let created = 0;
            for (const t of tasks) {
                const res = await fetch(`/api/projects/${targetProjectId}/tasks`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        title: t.title,
                        content: t.content,
                        task_type: "feature",
                        needs_plan_review: true,
                    }),
                });
                if (!res.ok) {
                    const detail = await res.json().catch(() => ({}));
                    throw new Error(detail.detail || res.statusText);
                }
                created++;
            }
            chatPlanEl.style.display = "none";
            appendMessage("assistant", `Created ${created} task${created !== 1 ? 's' : ''} with plan review enabled.`);
            currentPlan = null;
            currentPlanProjectId = null;
            lastTasksJson = null;
            loadTasks();
        } catch (err) {
            alert("Failed to create tasks: " + err.message);
        }
    }

    // ---- Plan Review Actions ----
    async function approvePlan(taskId) {
        if (!selectedProjectId) return;
        if (!confirm("Approve this plan and start execution?")) return;
        try {
            const res = await fetch(`/api/projects/${selectedProjectId}/tasks/${taskId}/approve-plan`, { method: "POST" });
            if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
            closeDetail();
            lastTasksJson = null;
            loadTasks();
        } catch (err) {
            alert("Failed to approve plan: " + err.message);
        }
    }

    function showReviseDialog() {
        const dialog = document.getElementById("revise-dialog");
        if (dialog) dialog.style.display = "block";
    }

    async function revisePlan(taskId) {
        if (!selectedProjectId) return;
        const feedbackEl = document.getElementById("revise-feedback");
        const feedback = feedbackEl ? feedbackEl.value.trim() : "";
        try {
            const res = await fetch(`/api/projects/${selectedProjectId}/tasks/${taskId}/revise-plan`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ feedback }),
            });
            if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
            closeDetail();
            lastTasksJson = null;
            loadTasks();
        } catch (err) {
            alert("Failed to revise plan: " + err.message);
        }
    }

    async function rejectPlan(taskId) {
        if (!selectedProjectId) return;
        if (!confirm("Reject this plan? The task will be marked as failed.")) return;
        try {
            const res = await fetch(`/api/projects/${selectedProjectId}/tasks/${taskId}/reject-plan`, { method: "POST" });
            if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
            closeDetail();
            lastTasksJson = null;
            loadTasks();
        } catch (err) {
            alert("Failed to reject plan: " + err.message);
        }
    }

    window._approvePlan = approvePlan;
    window._showReviseDialog = showReviseDialog;
    window._revisePlan = revisePlan;
    window._rejectPlan = rejectPlan;

    // Chat event listeners
    btnChatToggle.addEventListener("click", () => {
        chatSection.classList.toggle("collapsed");
    });

    btnChatClear.addEventListener("click", resetChat);
    btnSend.addEventListener("click", sendMessage);
    chatInput.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    btnTaskSubmit.addEventListener('click', submitTask);
    taskTitle.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            e.preventDefault();
            submitTask();
        }
    });

    document.getElementById("btn-plan-confirm").addEventListener("click", confirmPlan);
    document.getElementById("btn-plan-revise").addEventListener("click", () => {
        chatPlanEl.style.display = "none";
        currentPlan = null;
        chatInput.focus();
    });

    // ---- Sidebar Toggles (responsive) ----
    toggleLeft.addEventListener("click", () => sidebarLeft.classList.toggle("open"));
    toggleRight.addEventListener("click", () => sidebarRight.classList.toggle("open"));

    // ---- Collapsible panel toggle ----
    window.togglePanel = function (header) {
        header.classList.toggle("open");
        const content = header.nextElementSibling;
        content.classList.toggle("open");
    };

    // ---- Init & Polling ----
    loadProjects();
    setInterval(loadProjects, 30000);
    setInterval(() => {
        if (selectedProjectId) {
            loadTasks();
            loadWorktrees();
            loadCommits();
        }
    }, 15000);
})();
