# Stage 1: Task Tracker

## Scope

The workspace contains no application source or project specification, so Stage 1 uses a small generic task tracker. It will run locally with Python's standard library and a static browser UI. Tasks are held in memory for this stage.

## Behavior

- List tasks, create a task, mark it complete or active, and delete it.
- Require a non-empty trimmed title of at most 200 Unicode code points.
- Serve the browser UI and JSON API from one local service.
- Show loading, empty, success, and error states in the UI; use accessible labels and controls.

## Execution and handoffs

1. Backend Engineer: implement `server.py` and `tests/test_server.py`. Provide changed paths, API contract, validation examples, and build/test evidence.
2. Frontend Engineer: implement `index.html`, `app.js`, and `styles.css`. Use the shared API contract; provide changed paths and evidence for loading, empty, success, error, completion, and deletion flows.
3. Integrate the service and UI, then run the build and behavior checks.
4. Quality Reviewer: independently verify the integrated behavior and inspect edge cases. Report exact commands or steps, inputs, observed results, and any logs or screenshots.
5. Fix review defects and repeat affected checks. Stage 1 is complete only when the service builds and required behavior passes verification.
