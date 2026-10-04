(() => {
  const form = document.querySelector("#task-form");
  const input = document.querySelector("#task-title");
  const count = document.querySelector("#character-count");
  const formError = document.querySelector("#form-error");
  const region = document.querySelector("#task-region");
  const loading = document.querySelector("#loading-state");
  const errorState = document.querySelector("#error-state");
  const errorMessage = document.querySelector("#error-message");
  const emptyState = document.querySelector("#empty-state");
  const list = document.querySelector("#task-list");
  const retryButton = document.querySelector("#retry-button");
  const remainingCount = document.querySelector("#remaining-count");
  const remainingLabel = document.querySelector("#remaining-label");
  const completedSummary = document.querySelector("#completed-summary");
  const announcement = document.querySelector("#announcement");

  let tasks = [];
  let busyIds = new Set();
  let requestEpoch = 0;

  function titleCodePointLength(value) {
    return Array.from(value.trim()).length;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...options.headers },
    });
    if (!response.ok) {
      let message = `Request failed (${response.status}).`;
      try {
        const body = await response.json();
        if (body.error) message = body.error;
      } catch { /* Keep the useful status message for non-JSON errors. */ }
      throw new Error(message);
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function normalizeTasks(data) {
    const result = Array.isArray(data) ? data : data?.tasks;
    if (!Array.isArray(result)) throw new Error("The task service returned an unexpected response.");
    return result;
  }

  function normalizeTask(data) {
    const task = data?.task ?? data;
    if (!task || typeof task.id !== "string" || typeof task.title !== "string") {
      throw new Error("The task service returned an unexpected response.");
    }
    return task;
  }

  function showLoadState(state, message = "") {
    loading.hidden = state !== "loading";
    errorState.hidden = state !== "error";
    emptyState.hidden = state !== "empty";
    list.hidden = state !== "list";
    region.setAttribute("aria-busy", state === "loading" ? "true" : "false");
    if (message) errorMessage.textContent = message;
  }

  function render() {
    const pending = busyIds.size > 0;
    const active = tasks.filter((task) => !task.completed).length;
    const done = tasks.length - active;
    remainingCount.textContent = String(active);
    remainingLabel.textContent = active === 1 ? " task left" : " tasks left";
    completedSummary.textContent = done ? `${done} completed` : "";

    list.replaceChildren();
    for (const task of tasks) {
      const item = document.createElement("li");
      item.className = `task-item${task.completed ? " is-complete" : ""}`;
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "task-checkbox";
      checkbox.checked = Boolean(task.completed);
      checkbox.disabled = busyIds.has(task.id);
      checkbox.id = `task-${task.id}`;
      checkbox.setAttribute("aria-label", `${task.completed ? "Mark active" : "Complete"}: ${task.title}`);
      checkbox.addEventListener("change", () => toggleTask(task, checkbox.checked));

      const label = document.createElement("label");
      label.className = "task-title";
      label.htmlFor = checkbox.id;
      label.textContent = task.title;

      const meta = document.createElement("span");
      meta.className = "task-meta";
      meta.textContent = task.completed ? "DONE" : "TO DO";

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "delete-button";
      remove.disabled = busyIds.has(task.id);
      remove.setAttribute("aria-label", `Delete: ${task.title}`);
      remove.title = `Delete ${task.title}`;
      remove.textContent = "×";
      remove.addEventListener("click", () => deleteTask(task));

      item.append(checkbox, label, meta, remove);
      list.append(item);
    }
    if (pending) announcement.textContent = "Saving task changes.";
  }

  async function loadTasks() {
    const epoch = ++requestEpoch;
    showLoadState("loading");
    try {
      tasks = normalizeTasks(await request("/api/tasks"));
      if (epoch !== requestEpoch) return;
      if (!tasks.length) showLoadState("empty");
      else showLoadState("list");
      render();
    } catch (error) {
      if (epoch !== requestEpoch) return;
      showLoadState("error", error.message);
      announcement.textContent = "Could not load tasks.";
    }
  }

  async function toggleTask(task, completed) {
    busyIds.add(task.id);
    render();
    try {
      const updated = normalizeTask(await request(`/api/tasks/${encodeURIComponent(task.id)}`, {
        method: "PATCH", body: JSON.stringify({ completed }),
      }));
      tasks = tasks.map((item) => item.id === task.id ? updated : item);
      announcement.textContent = completed ? `Completed: ${task.title}` : `Reopened: ${task.title}`;
      showLoadState(tasks.length ? "list" : "empty");
    } catch (error) {
      announcement.textContent = `Could not update ${task.title}. ${error.message}`;
      formError.textContent = `Could not update “${task.title}”. ${error.message}`;
      formError.hidden = false;
    } finally {
      busyIds.delete(task.id);
      render();
    }
  }

  async function deleteTask(task) {
    busyIds.add(task.id);
    render();
    try {
      await request(`/api/tasks/${encodeURIComponent(task.id)}`, { method: "DELETE" });
      tasks = tasks.filter((item) => item.id !== task.id);
      announcement.textContent = `Deleted: ${task.title}`;
      showLoadState(tasks.length ? "list" : "empty");
    } catch (error) {
      announcement.textContent = `Could not delete ${task.title}. ${error.message}`;
      formError.textContent = `Could not delete “${task.title}”. ${error.message}`;
      formError.hidden = false;
    } finally {
      busyIds.delete(task.id);
      render();
    }
  }

  input.addEventListener("input", () => {
    count.textContent = `${titleCodePointLength(input.value)}/200`;
    formError.hidden = true;
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const title = input.value.trim();
    const titleLength = Array.from(title).length;
    if (!title) {
      formError.textContent = "Add a task name before saving.";
      formError.hidden = false;
      input.focus();
      return;
    }
    if (titleLength > 200) {
      formError.textContent = "Task names can be up to 200 characters.";
      formError.hidden = false;
      input.focus();
      return;
    }
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    formError.hidden = true;
    try {
      const created = normalizeTask(await request("/api/tasks", {
        method: "POST", body: JSON.stringify({ title }),
      }));
      tasks.push(created);
      input.value = "";
      count.textContent = "0/200";
      showLoadState("list");
      render();
      announcement.textContent = `Added: ${created.title}`;
      input.focus();
    } catch (error) {
      formError.textContent = error.message || "Could not add your task. Please try again.";
      formError.hidden = false;
      announcement.textContent = "Could not add task.";
    } finally {
      button.disabled = false;
    }
  });

  retryButton.addEventListener("click", loadTasks);
  loadTasks();
})();
