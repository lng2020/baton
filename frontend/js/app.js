// Baton — Single-layout dashboard controller
(function () {
    // ---- State ----
    let selectedProjectId = null;
    let projects = [];
    const statuses = ["pending", "in_progress", "completed", "failed"];

    // Polling cache — skip re-render when data unchanged
    let lastTasksJson = null;
    let lastWorktreesJson = null;
    let lastCommitsJson = null;

    // Chat state
    let chatHistory = [];
    let chatSessionId = null;
    let currentPlan = null;
    let isStreaming = false;

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

    const GREETING = "Hi! I'm your agent engineer. Describe what you'd like to accomplish, and I'll help you plan the tasks.";

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

        // Show chat section and reset conversation
        if (proj) {
            chatSection.style.display = "";
            resetChat();
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
        try {
            const res = await fetch(`/api/projects/${selectedProjectId}/tasks`);
            if (!res.ok) throw new Error(res.statusText);
            const tasks = await res.json();
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
                return `
                    <div class="task-card" onclick="window._openTaskDetail('${status}', '${escAttr(t.filename)}')">
                        <h4>${escHtml(t.title)}</h4>
                        <div class="task-meta">${escHtml(t.id)} &middot; ${modified}</div>
                        ${errorBadge}
                    </div>
                `;
            }).join("");
        }
    }

    // ---- Task Detail Panel ----
    function openTaskDetail(status, filename) {
        detailBody.innerHTML = '<div class="loading">Loading...</div>';
        detailOverlay.classList.add("open");
        detailPanel.classList.add("open");

        fetch(`/api/projects/${selectedProjectId}/tasks/${status}/${filename}`)
            .then(res => {
                if (!res.ok) throw new Error(res.statusText);
                return res.json();
            })
            .then(task => {
                renderDetail(task);
            })
            .catch(err => {
                detailBody.innerHTML = `<div class="empty-state">Failed to load task: ${escHtml(err.message)}</div>`;
            });
    }

    function closeDetail() {
        detailOverlay.classList.remove("open");
        detailPanel.classList.remove("open");
    }

    function renderDetail(task) {
        let html = `
            <h2>${escHtml(task.title)}</h2>
            <span class="detail-status ${task.status}">${task.status.replace("_", " ")}</span>
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
        try {
            const res = await fetch(`/api/projects/${selectedProjectId}/worktrees`);
            if (!res.ok) return;
            const worktrees = await res.json();
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
        try {
            const res = await fetch(`/api/projects/${selectedProjectId}/commits`);
            if (!res.ok) return;
            const commits = await res.json();
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
    function resetChat() {
        chatHistory = [];
        chatSessionId = null;
        currentPlan = null;
        isStreaming = false;
        chatMessages.innerHTML = `<div class="chat-message assistant"><div class="chat-bubble">${escHtml(GREETING)}</div></div>`;
        chatPlanEl.style.display = "none";
        chatInput.value = "";
        chatSection.classList.remove("collapsed");
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

    async function sendMessage() {
        const text = chatInput.value.trim();
        if (!text || !selectedProjectId || isStreaming) return;

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

            const res = await fetch(`/api/projects/${selectedProjectId}/chat`, {
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
                            bubble.textContent = "Error: " + data.error;
                            bubble.style.color = "var(--failed)";
                        } else if (data.type === "done") {
                            // Capture session_id for multi-turn
                            if (data.session_id) {
                                chatSessionId = data.session_id;
                            }
                            tryParsePlan(fullResponse);
                        }
                    } catch (_) { /* ignore malformed SSE */ }
                }
            }

            chatHistory.push({ role: "assistant", content: fullResponse });
        } catch (err) {
            bubble.textContent = "Error: " + err.message;
            bubble.style.color = "var(--failed)";
        } finally {
            isStreaming = false;
            btnSend.disabled = false;
        }
    }

    function tryParsePlan(text) {
        if (!text.includes('"plan"') || !text.includes('"tasks"')) return;

        // Find the opening brace — prefer after ```json marker if present
        let searchFrom = 0;
        const marker = text.indexOf("```json");
        if (marker !== -1) searchFrom = marker + 7;

        const braceStart = text.indexOf("{", searchFrom);
        if (braceStart === -1) return;

        // Try parsing from braceStart to each closing brace, outermost first
        let end = text.lastIndexOf("}");
        while (end > braceStart) {
            try {
                const plan = JSON.parse(text.substring(braceStart, end + 1));
                if (plan.plan && plan.tasks && plan.tasks.length) {
                    currentPlan = plan;
                    showPlan(plan);
                    return;
                }
            } catch (_) { /* try shorter substring */ }
            end = text.lastIndexOf("}", end - 1);
        }
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
        if (!currentPlan || !selectedProjectId) return;
        const tasks = currentPlan.tasks.map(t => ({ title: t.title, content: t.content }));
        try {
            const res = await fetch(`/api/projects/${selectedProjectId}/tasks/bulk`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ tasks }),
            });
            if (!res.ok) throw new Error(res.statusText);
            resetChat();
            loadTasks();
        } catch (err) {
            alert("Failed to create tasks: " + err.message);
        }
    }

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
