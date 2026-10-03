import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import vm from "node:vm";

const appSource = await readFile(
  fileURLToPath(new URL("../web/app.js", import.meta.url)),
  "utf8",
);
const htmlSource = await readFile(
  fileURLToPath(new URL("../web/index.html", import.meta.url)),
  "utf8",
);

class Element {
  constructor() {
    this.hidden = false;
    this.value = "";
    this.textContent = "";
    this.disabled = false;
    this.checked = false;
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
  }

  addEventListener(name, handler) {
    this.listeners.set(name, handler);
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  append(...elements) {
    this.children.push(...elements);
  }

  replaceChildren(...elements) {
    this.children = [...elements];
  }

  querySelector(selector) {
    return selector === "button[type=submit]" ? this.submitButton : null;
  }

  focus() {}
}

class Response {
  constructor(status, body) {
    this.status = status;
    this.ok = status >= 200 && status < 300;
    this.body = body;
  }

  async json() {
    return this.body;
  }
}

class Harness {
  constructor(firstGetStatus = 200) {
    this.ids = [
      "task-form", "task-title", "character-count", "form-error", "task-region",
      "loading-state", "error-state", "error-message", "empty-state", "task-list",
      "retry-button", "remaining-count", "remaining-label", "completed-summary",
      "announcement",
    ];
    this.elements = new Map(this.ids.map((id) => [`#${id}`, new Element()]));
    this.elements.get("#task-form").submitButton = new Element();
    this.tasks = [];
    this.firstGetStatus = firstGetStatus;
    this.releaseInitialGet = null;
    this.getCount = 0;
    this.nextId = 1;
    this.document = {
      querySelector: (selector) => this.elements.get(selector),
      createElement: () => new Element(),
    };
    this.fetch = (path, options = {}) => this.handleRequest(path, options);
  }

  async handleRequest(path, options) {
    const method = options.method ?? "GET";
    if (path === "/api/tasks" && method === "GET") {
      this.getCount += 1;
      if (this.getCount === 1) {
        return new Promise((resolve) => {
          this.releaseInitialGet = () => resolve(
            this.firstGetStatus === 200
              ? new Response(200, { tasks: this.tasks.map((task) => ({ ...task })) })
              : new Response(this.firstGetStatus, { error: "Service unavailable" }),
          );
        });
      }
      return new Response(200, { tasks: this.tasks.map((task) => ({ ...task })) });
    }

    if (path === "/api/tasks" && method === "POST") {
      const payload = JSON.parse(options.body);
      const task = {
        id: String(this.nextId++),
        title: payload.title.trim(),
        completed: false,
        created_at: "2026-10-03T00:00:00Z",
      };
      this.tasks.push(task);
      return new Response(201, { task });
    }

    const match = path.match(/^\/api\/tasks\/(.+)$/);
    const id = match?.[1] ? decodeURIComponent(match[1]) : null;
    const task = this.tasks.find((item) => item.id === id);
    if (!task) return new Response(404, { error: "Task not found" });
    if (method === "PATCH") {
      Object.assign(task, JSON.parse(options.body));
      return new Response(200, { task });
    }
    if (method === "DELETE") {
      this.tasks = this.tasks.filter((item) => item.id !== id);
      return new Response(204, null);
    }
    return new Response(405, { error: "Method not allowed" });
  }

  start() {
    vm.runInNewContext(appSource, {
      document: this.document,
      fetch: this.fetch,
      encodeURIComponent,
      Set,
      Error,
      JSON,
    });
  }

  async flush() {
    await new Promise((resolve) => setImmediate(resolve));
  }

  get(id) {
    return this.elements.get(`#${id}`);
  }
}

test("task UI loads, validates, creates, completes, deletes, and returns to empty", async () => {
  const app = new Harness();
  app.start();

  assert.equal(app.get("loading-state").hidden, false, "loading state is visible before the API responds");
  app.releaseInitialGet();
  await app.flush();
  assert.equal(app.get("empty-state").hidden, false);
  assert.equal(app.get("task-region").attributes.get("aria-busy"), "false");

  const input = app.get("task-title");
  input.value = "   ";
  await app.get("task-form").listeners.get("submit")({ preventDefault() {} });
  assert.equal(app.get("form-error").hidden, false);
  assert.equal(app.get("form-error").textContent, "Add a task name before saving.");
  assert.equal(app.tasks.length, 0);

  input.value = "  Draft plan  ";
  await app.get("task-form").listeners.get("submit")({ preventDefault() {} });
  assert.equal(app.get("task-list").hidden, false);
  assert.equal(app.get("task-list").children.length, 1);
  assert.equal(app.get("task-list").children[0].children[1].textContent, "Draft plan");
  assert.equal(app.get("remaining-count").textContent, "1");

  const row = app.get("task-list").children[0];
  const checkbox = row.children[0];
  checkbox.checked = true;
  await checkbox.listeners.get("change")();
  assert.equal(app.get("task-list").children[0].children[2].textContent, "DONE");
  assert.equal(app.get("remaining-count").textContent, "0");

  const deleteButton = app.get("task-list").children[0].children[3];
  await deleteButton.listeners.get("click")();
  assert.equal(app.get("empty-state").hidden, false);
  assert.equal(app.tasks.length, 0);
  assert.match(app.get("announcement").textContent, /Deleted: Draft plan/);
});

test("task UI exposes a load error and retry recovers to the empty state", async () => {
  const app = new Harness(503);
  app.start();
  assert.equal(app.get("loading-state").hidden, false);
  app.releaseInitialGet();
  await app.flush();

  assert.equal(app.get("error-state").hidden, false);
  assert.equal(app.get("error-message").textContent, "Service unavailable");

  await app.get("retry-button").listeners.get("click")();
  assert.equal(app.get("error-state").hidden, true);
  assert.equal(app.get("empty-state").hidden, false);
  assert.equal(app.get("task-list").hidden, true);
});

test("client accepts and counts 200 emoji as 200 code points", async () => {
  const app = new Harness();
  app.start();
  app.releaseInitialGet();
  await app.flush();

  const input = app.get("task-title");
  input.value = "😀".repeat(200);
  input.listeners.get("input")();
  assert.equal(app.get("character-count").textContent, "200/200");

  await app.get("task-form").listeners.get("submit")({ preventDefault() {} });
  assert.equal(app.tasks.length, 1);
  assert.equal(Array.from(app.tasks[0].title).length, 200);
  assert.equal(app.get("form-error").hidden, true);
});

test("client counts and rejects 201 emoji as over the 200 code point limit", async () => {
  const app = new Harness();
  app.start();
  app.releaseInitialGet();
  await app.flush();

  const input = app.get("task-title");
  input.value = "😀".repeat(201);
  input.listeners.get("input")();
  assert.equal(app.get("character-count").textContent, "201/200");

  await app.get("task-form").listeners.get("submit")({ preventDefault() {} });
  assert.equal(app.get("form-error").hidden, false);
  assert.equal(app.get("form-error").textContent, "Task names can be up to 200 characters.");
  assert.equal(app.tasks.length, 0);
});

test("task title input does not apply the browser's UTF-16 maxlength", () => {
  assert.doesNotMatch(htmlSource, /<input\b[^>]*\bid="task-title"[^>]*\bmaxlength=/i);
});
