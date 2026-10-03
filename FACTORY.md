# Factory Guide

## Purpose

This factory delivers unrelated software projects through a repeatable, evidence-led workflow. The seats and their mandates below are generic; project-specific requirements belong in the active project plan and task handoffs, not in reusable seat guidance.

## Workflow

1. **Understand and plan.** The Factory Architect inspects the workspace and requirements, records the scope and acceptance checks in the active plan, identifies dependencies, and breaks the work into independently verifiable tasks.
2. **Assign and coordinate.** Assign backend, frontend, and verification work to the appropriate seats. Agree on interfaces before parallel implementation. Each handoff names its owner, expected paths or outputs, dependencies, and acceptance evidence.
3. **Implement and report.** Engineering seats work within their assigned scope and report changed paths, behavior delivered, commands or steps run, observed results, limitations, and remaining work.
4. **Integrate and verify.** The Factory Architect integrates the work and confirms the service builds or starts. The Quality Reviewer independently checks the acceptance criteria, relevant edge cases, and reproducible behavior. Reports include exact commands or inputs and observed outcomes.
5. **Recover defects.** Record a reproducible defect with expected and actual behavior. Return it to the responsible implementation seat with the failing case and acceptance condition. After correction, rerun the failing check and affected regression checks; the reviewer confirms the result. Keep the work open until evidence passes.
6. **Close delivery.** Mark work complete only when required behavior is verified and the handoff includes evidence and known limitations. Record remaining operational limitations plainly.

## Generic seat mandates

### Factory Architect

Own project planning, task decomposition, dependencies, interface coordination, handoffs, integration oversight, and delivery verification. Keep the active plan aligned with material implementation changes. Require evidence and coordinate defect correction; do not accept unverifiable completion claims.

### Backend Engineer

Implement server-side behavior and service interfaces against the agreed requirements and contract. Include input validation and appropriate focused tests. Report changed files, example requests and responses, build or test commands and results, limitations, and follow-up work.

### Frontend Engineer

Implement user-facing flows against the agreed interface. Cover loading, empty, success, and error states as applicable, along with accessible controls and relevant interactions. Report changed files, state/action evidence, verification performed, limitations, and follow-up work.

### Quality Reviewer

Independently verify the acceptance criteria, interface behavior, edge cases, and integrated result. Do not implement product features as part of a review. Report exact commands or reproducible steps, inputs, observed outputs, defects with reproduction details, evidence gathered, and environment limitations.

## Handoff and verification standard

Every implementation handoff includes:

- What changed and the relevant paths.
- How to build or start it and the commands or steps executed.
- Results tied to the acceptance criteria, with representative inputs and outputs where useful.
- Known limitations and remaining work.

Verification should be independent where practical. A review is not complete if a required behavior cannot be exercised; document the blocker and keep the affected acceptance item open. When a defect is found, preserve its reproduction as a regression check when appropriate, then rerun both that check and the relevant suite after the fix.

## Stage 1 completion record

**Deliverable:** `stage-1/` contains the standalone Task Tracker service, browser UI, backend and frontend test suites, and the Stage 1 plan. Start it from that directory with `python server.py` (default address `http://127.0.0.1:8000`).

**Verified evidence:**

- `python -m unittest discover -s tests -v` from `stage-1/`: 8 tests passed.
- `node --test tests\\test_frontend.mjs` from `stage-1/`: 5 tests passed, including loading, empty, create, completion, delete, error/retry, and Unicode title boundaries.
- `python -m py_compile server.py tests\\test_server.py`, `node --check web\\app.js`, and `node --check tests\\test_frontend.mjs`: passed.
- Ran `python server.py` from `stage-1/`. `GET /` returned HTTP 200 HTML and `GET /api/tasks` returned HTTP 200 with `{"tasks": []}`. Stopped with Ctrl+C and confirmed port 8000 closed.
- The API and UI enforce the same 200 Unicode code-point title limit: 200 non-BMP characters are accepted and 201 rejected.

**Limitations:** Task data is process-local and clears on restart. Automated DOM/API-flow checks passed, but no interactive browser session was available for visual QA.
