# DAMapQL Workflow Architecture & Integration Guide

This document describes the structure of the **DAMapQL Workflow Engine** and the procedure for onboarding a new application, using the **GeoCad Task-based QC Workflow (with Token Control)** as a concrete reference.

---

## 1. Architectural Overview & Design Pattern

The workflow engine follows the same **Inversion of Control (IoC) / Plugin Pattern** established across DAMapQL for `BaseBoundaryHierarchy` and `BaseUserAOI`.

* **DAMapQL (Core Engine)** acts as the generic state-machine orchestrator. It manages state transitions, audit logging, role checking, spatial boundary scoping, and optional 2FA / token validation. It contains **no hardcoded business domain knowledge** (e.g., it does not know what an "ArcGIS task", a "manifest", or a "society submission" is).
* **Host Applications (e.g., GeoCad, WASA, LBDC)** define their own state transition graphs, access rules, required parameters, and execution hooks.
* **Composition at Bootstrap**: Concrete workflow definitions are registered into DAMapQL's registry during FastAPI application bootstrap via `app_extension.py`.



---

## 2. Directory Structure

```text
project-root/
├── app/
│   ├── damapql/                             # Core Reusable Engine
│   │   ├── auth/                            # UserInfo, Roles, Permissions, 2FA
│   │   └── config_registry/                 # Generic Extension Subsystem
│   │       ├── base.py                      # BaseConfigRegistry[T] generic contract
│   │       ├── aoi/                         # Spatial Area of Interest resolution
│   │       ├── boundaries/                  # Multi-tier organizational navigation
│   │       └── workflows/                   # ⬅️ Core Workflow Subsystem
│   │           ├── __init__.py              # Central exports & combined router mount
│   │           ├── base.py                  # BaseWorkflowRegistry & NullWorkflowRegistry
│   │           ├── schemas.py               # WorkflowDefinition, Transition, State schemas
│   │           ├── models.py                # Generic WorkflowInstance & WorkflowAuditLog
│   │           ├── service.py               # Transition validation, RBAC, 2FA, execution
│   │           └── router.py                # Unified API endpoints (/api/config/workflows/*)
│   │
│   └── geocad/                              # ⬅️ Host Application
│       ├── config/
│       │   └── workflows.py                 # Concrete Workflow Definition (States & Transitions)
│       ├── services/
│       │   └── qc_task_service.py           # Domain actions: checkout, ZIP packaging, token verify
│       ├── routers/
│       │   └── task_router.py               # Domain endpoints: download task, upload edited data
│       └── app_extension.py                 # Application bootstrap & registry injection

```

---

## 3. Core Engine Components (`damapql/config_registry/workflows/`)

### A. Schemas (`schemas.py`)

Provides the declarative contracts for workflows:

* **`WorkflowStateConfig`**: Identifies a state (e.g., `ASSIGNED`, `IN_PROGRESS`, `UNDER_REVIEW`), whether it is initial or terminal, and its UI badge color token.
* **`WorkflowTransitionConfig`**: The directed edge between two states. Defines:
* Source (`from_state`) and target (`to_state`).
* Action identifier and human-readable label.
* Access guards: `required_roles`, `required_department`, `requires_superuser`.
* Spatial guards: `enforce_district_scope`, `enforce_subdivision_scope`.
* Security gates: `requires_2fa`, `two_factor_permission`.
* Requirements: `requires_comment`, notification email templates.


* **`WorkflowDefinition`**: Container bundling states and transitions under a unique `workflow_key`.
* **`ExecuteTransitionRequest`**: Standardized payload accepting the action, comments, custom payload (e.g., tokens, versions), and optional 2FA/OTP values.

### B. Registry Contract (`base.py`)

* Inherits from `BaseConfigRegistry`.


* Maintains a class-level registry where host applications inject their concrete workflow definitions during startup.
* Provides retrieval methods (`get_workflow(workflow_key)`) used by the execution service.

### C. Database Models (`models.py`)

Two generic tables handle workflow tracking for any domain entity:

* **`WorkflowInstance`**: Polymorphic tracker storing `workflow_key`, `entity_type` (e.g., `"cadastre_task"`), `entity_id` (e.g., task ID `1025`), current state, spatial references (`dist_id`, `subdiv_id`), creator, and timestamps.
* **`WorkflowAuditLog`**: Append-only log recording every transition, source state, target state, action performed, executing user ID, timestamp, comments, and extra JSON metadata.

### D. Engine Service (`service.py`)

Evaluates guards and commits state transitions:

1. **State Validation**: Confirms the requested action is permitted from the current instance state.
2. **Identity & RBAC**: Matches the executing `UserInfo` roles against the transition's `required_roles`.
3. **Spatial Jurisdiction**: Checks whether the task's spatial boundary matches the user's operational scope if spatial boundaries are enforced.
4. **Security / 2FA Gate**: Verifies time-step OTPs via `GoogleAuthenticatorManager` when flagged.
5. **Mutation & Audit**: Updates the current state on `WorkflowInstance`, records the transition in `WorkflowAuditLog`, and triggers background notification emails.

### E. Unified API Router (`router.py`)

Standard endpoints mounted under `/api/config/workflows/`:

* `GET /instance/{id}/allowed_actions/`: Dynamically returns permitted actions for the current user and state (used by `@damap` frontend panels to render buttons).
* `POST /instance/{id}/transition/`: Executes an allowed transition.
* `GET /instance/{id}/history/`: Retrieves the immutable audit trail for inspection.

---

## 4. Implementation Guide: Adding a New Application (GeoCad Example)

The **GeoCad Task-based QC Workflow** demonstrates how to register an application-specific workflow with domain operations (token validation, task packaging, and review).

### Step 1: Map the Workflow Lifecycle

From the GeoCad workflow specification:

```text
[Assigned] ──────── (Download Task) ────────► [In Progress]
    ▲                                                │
    │ (Reopen Request)                               │ (Upload Edited Data)
    │                                                ▼
[Accepted (Locked)] ◄─── (Accept) ──── [Under Review]
                                │                    │
                                └── (Reject/Return) ─┴─► [Returned]
                                                           │
                                                           └── (Re-upload) ──► [Under Review]

```

* **Roles Involved**:
* `Cadastre Manager` (`app_manager`, `survey_manager`): Assigns tasks, reviews submissions, accepts/locks, rejects, or reopens.
* `Assigned Editor` (`data_editor`): Downloads task, edits in ArcGIS, uploads package with signed token.


* **States**:
* `ASSIGNED` (Initial)
* `IN_PROGRESS`
* `UNDER_REVIEW`
* `RETURNED`
* `ACCEPTED` (Terminal / Read-only)



---

### Step 2: Define the Concrete Workflow in GeoCad

Create `app/geocad/config/workflows.py`:

1. Subclass `BaseWorkflowRegistry`.
2. Define the states with display labels and UI badge colors.
3. Configure each transition with required role guards:
* **`DOWNLOAD_CHECKOUT`** (Transition from `ASSIGNED` or `RETURNED` to `IN_PROGRESS`): Restricted to `data_editor`.
* **`SUBMIT_FOR_REVIEW`** (Transition from `IN_PROGRESS` to `UNDER_REVIEW`): Restricted to `data_editor`. Enforces payload checks (token, version).
* **`ACCEPT_TASK`** (Transition from `UNDER_REVIEW` to `ACCEPTED`): Restricted to `cadastre_manager` / `survey_manager`. Locks the task.
* **`REJECT_TASK`** (Transition from `UNDER_REVIEW` to `RETURNED`): Restricted to `cadastre_manager`. Sets `requires_comment = True` so review feedback is mandatory.
* **`REOPEN_TASK`** (Transition from `ACCEPTED` to `ASSIGNED`): Manager approval action to permit re-editing completed tasks.



---

### Step 3: Implement Domain Actions (Tokens & File Packaging)

While DAMapQL manages state transitions and audit logs, GeoCad manages file transformations and token verification in its own service layer (`app/geocad/services/qc_task_service.py`):

1. **Checkout Packaging (`Download Task`)**:
* Generates a signed checkout token (e.g., `T001`) with expiration and editor ID.
* Bundles the target shapefile (`parcels.shp`, `.shx`, `.dbf`, `.prj`) and the `geocad_manifest.json` containing the encoded signature into a checkout ZIP.
* Invokes `WorkflowEngineService.execute_transition` to shift state from `ASSIGNED` to `IN_PROGRESS`.


2. **Upload & Manifest Verification (`Upload Task`)**:
* Inspects the uploaded ZIP package for the signed manifest.
* Verifies the token signature, checks that the token is active and not already consumed, and validates that the task ID and editor match.
* Consumes the token to prevent replay attacks.
* Invokes `WorkflowEngineService.execute_transition` with action `SUBMIT_FOR_REVIEW` and records the submission version in `extra_payload`.



---

### Step 4: Register During Application Bootstrap

Connect the GeoCad extension to DAMapQL in `app/geocad/app_extension.py`:

1. Import `BaseWorkflowRegistry` and `config_registry_router` from `app.damapql.config_registry`.
2. Import the concrete `GeoCadWorkflowRegistry` and domain routers (`geocad_task_router`).
3. In `configure_geocad_extension(app: FastAPI)`:
* Call `BaseWorkflowRegistry.register_implementation(GeoCadWorkflowRegistry)`.
* Mount the shared `config_registry_router` under `/api` (exposing `/api/config/workflows/*`).
* Mount GeoCad application routers (exposing task download and upload endpoints).



---

### Step 5: Enable the Extension in `settings.py`

In the host project's configuration file, register the extension callable:

```python
DAMAPQL_APP_EXTENSIONS = [
    "app.geocad.app_extension:configure_geocad_extension",
]

```

When `create_app()` executes at startup:

1. DAMapQL initializes core services and database connections.
2. The bootstrap loader dynamically imports and executes `configure_geocad_extension`.
3. The GeoCad QC workflow is registered in memory.
4. The frontend `@damap` workflow actions panel queries `/api/config/workflows/instance/{task_id}/allowed_actions/` and immediately receives the allowed buttons (e.g., Download, Submit, Accept, Reject) matching the user's role and current task status.