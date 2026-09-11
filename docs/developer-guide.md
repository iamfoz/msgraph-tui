# Graphdeck developer guide

## Layout

```
src/msgraph_tui/
├── core/          contracts: actions.py (registry), envelope.py, errors.py,
│                  redaction.py, providers.py (engine interface), selection.py,
│                  config.py, export.py
├── providers/     graph_request.py (pure builder) + graph_rest.py,
│                  ps_builder.py (pure builder/parser) + powershell.py,
│                  graph_sdk.py (stub), mock.py
├── compliance/    audit.py (hash chain), rollback.py, evidence.py
├── modules/       users.py, groups.py, licenses.py — ActionDefinitions only
├── services/      executor.py (write pipeline), context.py (wiring)
├── app/           main.py (shell), views.py, modals.py
└── fixtures/      mock tenant JSON
```

Design rules:

1. **UI never touches providers.** Screens call the `Executor`; everything a
   screen shows comes from registry metadata or a `ResultEnvelope`.
2. **All output paths are redacted** via `core.redaction` — previews, logs,
   audit records, snapshots, exports.
3. **Write behaviour is data.** Risk, confirmation level, rollback spec and
   audit requirement live on the `ActionDefinition`; the `Executor` enforces
   them and the registry rejects write actions that omit them.
4. **Pure builders.** Request/command construction (`graph_request.py`,
   `ps_builder.py`) does no I/O so it is exhaustively unit-testable.

## Adding a new action

Add an `ActionDefinition` in the relevant module (or a new one):

```python
registry.register(ActionDefinition(
    id="users.set_usage_location",
    name="Set usage location",
    description="Set the ISO country code required before licence assignment.",
    service="Entra ID",
    preferred_provider="graph_rest",
    supported_providers=["graph_rest", "graph_powershell", "mock"],
    risk=RiskLevel.LOW,
    params=[
        ParamSpec("user_id", required=True),
        ParamSpec("usageLocation", required=True, description="e.g. GB"),
    ],
    graph=GraphTemplate(
        method="PATCH", path="/users/{user_id}",
        body={"usageLocation": "{usageLocation}"}, paginate=False,
    ),
    powershell=PowerShellTemplate(
        module="Microsoft.Graph.Users", command="Update-MgUser",
        parameters=[
            PSParamSpec("UserId", source="user_id"),
            PSParamSpec("UsageLocation", source="usageLocation"),
        ],
        supports_whatif=True,
    ),
    graph_scopes=["User.ReadWrite.All"],
    confirmation=Confirmation.CONFIRM,
    audit=AuditRequirement.CHANGE_LOG,
    rollback=RollbackSpec(
        level=RollbackLevel.FULL,
        read_action="users.get",
        read_param_map={"user_id": "user_id"},
        tracked_fields=["usageLocation"],
        build_inverse=lambda p, before: (
            "users.set_usage_location",
            {"user_id": p["user_id"], "usageLocation": before.get("usageLocation")},
        ),
    ),
    docs_url="https://learn.microsoft.com/graph/api/user-update",
))
```

Checklist for every write action (the registry enforces most of this):

- [ ] `confirmation` is `CONFIRM` or `TYPED` (TYPED for high-risk/destructive)
- [ ] `audit=AuditRequirement.CHANGE_LOG`
- [ ] `rollback` classified; HIGH/DESTRUCTIVE with `RollbackLevel.NONE` must
      say why in `RollbackSpec.notes`
- [ ] a mock handler exists (`providers/mock.py`) so it is testable offline
- [ ] tests: builder output, mock execution, rollback round-trip if supported
- [ ] `docs_url` points at the Microsoft reference

Then surface it: add a `RowOp` to a `BrowseSpec` in `app/main.py`, or a new
`BrowseSpec` + navigation leaf for a new list view.

## Adding a new provider engine

Implement `core.providers.Provider`:

```python
class MyProvider(Provider):
    name = "my_provider"
    display_name = "My provider"
    def is_available(self) -> bool: ...
    def preview(self, action, params) -> OperationPreview: ...
    async def execute(self, action, params) -> ResultEnvelope: ...
```

Rules:

- `execute` must never raise for provider-side failures — normalise them into
  `envelope.errors` using the `ErrorCategory` taxonomy and attach `guidance`
  (use `errors.with_guidance`).
- Fill `request_preview` with the redacted representation of exactly what ran.
- Register it in `services/context.build_context`, add the provider id to the
  `supported_providers` of relevant actions, and document it in
  `docs/capability-matrix.md` (including its limitations — honesty about
  coverage is a product requirement).

## Adding a new admin module

1. Create `modules/<area>.py` with a `register(registry)` function; add it to
   `modules.build_registry`.
2. Add mock fixtures + handlers so the module works offline.
3. Add `BrowseSpec`s and a navigation node.
4. Add a section to the capability matrix and tests.

## Testing

```bash
pip install '.[dev]'
python -m pytest            # full suite; no tenant, no PowerShell, no network
```

- Engine tests use `httpx.MockTransport` (Graph) and pure builders (PowerShell).
- `tests/conftest.py` provides `mock_ctx` / `dry_run_ctx` — a fully wired app
  context with isolated state under `tmp_path`.
- UI smoke tests run headless via Textual's `run_test` pilot.
- Screenshots: `docs/screenshot-*.svg` are generated with
  `App.save_screenshot` from a pilot session.

## Release notes for maintainers

- Secrets: never log tokens; the `Authorization` header is constructed in one
  place (`graph_rest._run`) and must stay out of envelopes (tested).
- The audit log format is append-only; never rewrite entries — new fields go
  into new entries only, and `verify()` must keep passing on old files.
- Rollback snapshots are forward-compatible: `RollbackSnapshot.from_dict`
  drops unknown keys.
