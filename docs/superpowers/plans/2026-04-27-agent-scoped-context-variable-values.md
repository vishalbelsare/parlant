# Agent-scoped Context Variable Values Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an "agent (by id)" tier to the context-variable value-resolution chain so the same variable can carry per-agent defaults, with SDK methods on `Variable` to set/get them.

**Architecture:** No store schema change. Agent-tier values are stored in the existing `_value_collection` keyed by `Tag.for_agent_id(agent.id).id` (i.e., the literal string `"agent:{agent_id}"`, distinct from the `"tag:{...}"` keys used for customer-tag values). The engine inserts a single new key into its precedence list between the customer-tag tier and the global tier; `_load_context_variable_value` and the tool-based fallback are unaffected. The SDK exposes two new methods on the `Variable` dataclass that mirror the existing `set_value_for_customer` / `set_value_for_tag` / `set_global_value` pattern.

**Tech Stack:** Python 3, MyPy strict, pytest, ruff, the project's existing `SDKTest` harness.

**Spec:** `docs/superpowers/specs/2026-04-27-agent-scoped-context-variable-values-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/parlant/sdk.py` | Modify (around 2942–2996) | Add `Variable.set_value_for_agent` and `Variable.get_value_for_agent`. |
| `src/parlant/core/engines/alpha/engine.py` | Modify (1101–1105 + imports) | Insert agent-tier key into precedence list; add `Tag` import. |
| `tests/sdk/test_variables.py` | Modify (append) | Three new `SDKTest` classes covering SDK round-trip, engine resolution of agent tier, and customer-tag-vs-agent precedence regression guard. |

No new files. No store, schema, migration, or API-layer changes.

---

## Task 1: SDK methods on `Variable` for agent-scoped values

**Files:**
- Modify: `src/parlant/sdk.py:2942-2996` (add two methods to the `Variable` dataclass)
- Test: `tests/sdk/test_variables.py` (append a new class)

**Why test first:** Per project TDD policy (`CLAUDE.md`), we add a failing test that exercises the new SDK surface before implementing it. This test is a pure round-trip through the store via the new SDK methods — it does not need the engine change yet.

- [ ] **Step 1: Append the failing test to `tests/sdk/test_variables.py`**

Append at the end of the file:

```python
class Test_that_a_variable_value_can_be_set_for_an_agent(SDKTest):
    async def setup(self, server: p.Server) -> None:
        self.agent = await server.create_agent(
            name="Var Agent",
            description="Agent for variable per-agent value test",
        )

        self.variable = await self.agent.create_variable(
            name="subscription_plan",
            description="The current subscription plan of the user.",
        )

        await self.variable.set_value_for_agent(self.agent, "premium")

    async def run(self, ctx: Context) -> None:
        assert "premium" == await self.variable.get_value_for_agent(self.agent)
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `uv run pytest tests/sdk/test_variables.py::Test_that_a_variable_value_can_be_set_for_an_agent -v`

Expected: FAIL — `AttributeError: 'Variable' object has no attribute 'set_value_for_agent'` (raised inside `setup`).

- [ ] **Step 3: Add the two methods to `Variable` in `src/parlant/sdk.py`**

Locate the `Variable` dataclass (around line 2926). It already imports `Tag as _Tag` at line 272 and uses `_Tag.for_agent_id(...)` elsewhere in the file (e.g., line 3265), so no new imports are needed.

Insert these two methods immediately **after** `set_global_value` and **before** `get_value_for_customer` (i.e., between current lines 2967 and 2969 — the natural symmetric position alongside the existing `set_*` / `get_*` siblings):

```python
    async def set_value_for_agent(self, agent: Agent, value: JSONSerializable) -> None:
        """Sets the value of the variable for a specific agent."""

        await self._container[ContextVariableStore].update_value(
            variable_id=self.id,
            key=_Tag.for_agent_id(agent.id).id,
            data=value,
        )

    async def get_value_for_agent(self, agent: Agent) -> JSONSerializable | None:
        """Retrieves the value of the variable for a specific agent."""

        value = await self._container[ContextVariableStore].read_value(
            variable_id=self.id,
            key=_Tag.for_agent_id(agent.id).id,
        )

        return value.data if value else None
```

After insertion, the `Variable` class should contain (in order): `set_value_for_customer`, `set_value_for_tag`, `set_global_value`, `set_value_for_agent`, `get_value_for_customer`, `get_value_for_tag`, `get_global_value`, `get_value_for_agent`, `get_value`.

- [ ] **Step 4: Run the test and confirm it passes**

Run: `uv run pytest tests/sdk/test_variables.py::Test_that_a_variable_value_can_be_set_for_an_agent -v`

Expected: PASS.

- [ ] **Step 5: Format and commit**

```bash
uv run ruff format src/parlant/sdk.py tests/sdk/test_variables.py
git add src/parlant/sdk.py tests/sdk/test_variables.py
git commit -m "$(cat <<'EOF'
Add Variable.set_value_for_agent / get_value_for_agent to SDK

Mirrors the existing per-customer / per-tag / global setters. Stores
the value under key Tag.for_agent_id(agent.id).id in the existing
context-variable values store; no schema change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Engine reads the agent tier between customer-tag and global

**Files:**
- Modify: `src/parlant/core/engines/alpha/engine.py` (imports + `_load_context_variables` around line 1101)
- Test: `tests/sdk/test_variables.py` (append a new class)

**Why test first:** This test asserts the engine actually consults the new key during a turn. Before the engine change, the engine's precedence list has no agent tier, so a customer with no per-customer / per-tag value will fall through to the global value, never reading the agent value — that's the failure we want to demonstrate first.

- [ ] **Step 1: Append the failing test to `tests/sdk/test_variables.py`**

Append at the end of the file:

```python
class Test_that_variable_value_for_agent_is_used_when_no_customer_or_tag_value_exists(SDKTest):
    async def setup(self, server: p.Server) -> None:
        self.agent = await server.create_agent(
            name="Var Agent",
            description="Agent for variable per-agent engine resolution test",
        )

        self.customer = await server.create_customer("Jane Doe")

        self.variable = await self.agent.create_variable(
            name="subscription_plan",
            description="The current subscription plan of the user.",
        )

        await self.variable.set_global_value("free")
        await self.variable.set_value_for_agent(self.agent, "premium")

        self.retrieved_value: p.JSONSerializable | None = None

        variable = self.variable

        async def custom_retriever(ctx: p.RetrieverContext) -> p.RetrieverResult:
            self.retrieved_value = await variable.get_value()
            return p.RetrieverResult(data={"plan": self.retrieved_value})

        await self.agent.attach_retriever(custom_retriever)

    async def run(self, ctx: Context) -> None:
        await ctx.send_and_receive_message(
            customer_message="What is my subscription plan?",
            recipient=self.agent,
            sender=self.customer,
        )

        assert self.retrieved_value == "premium", (
            f"Expected agent-tier value 'premium', got {self.retrieved_value!r}"
        )
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `uv run pytest tests/sdk/test_variables.py::Test_that_variable_value_for_agent_is_used_when_no_customer_or_tag_value_exists -v`

Expected: FAIL with assertion `Expected agent-tier value 'premium', got 'free'` — the engine's current precedence list has no agent tier, so the resolver falls through customer→customer-tags→GLOBAL and returns the global value `"free"`.

- [ ] **Step 3: Add the `Tag` import to `src/parlant/core/engines/alpha/engine.py`**

`engine.py` does not currently import `Tag`. Add the following import line, placed alphabetically among the existing `from parlant.core.…` imports (a reasonable spot is just before `from parlant.core.tools …` if present, or right after the `from parlant.core.sessions import (…)` block — match the file's existing ordering):

```python
from parlant.core.tags import Tag
```

- [ ] **Step 4: Modify the precedence list in `_load_context_variables`**

Locate `_load_context_variables` (around line 1089). Current code at lines 1101–1105:

```python
        keys_to_check_in_order_of_importance = (
            [context.customer.id]  # Customer-specific value
            + [f"tag:{tag_id}" for tag_id in context.customer.tags]  # Tag-specific value
            + [ContextVariableStore.GLOBAL_KEY]  # Global value
        )
```

Replace with:

```python
        keys_to_check_in_order_of_importance = (
            [context.customer.id]  # Customer-specific value
            + [f"tag:{tag_id}" for tag_id in context.customer.tags]  # Tag-specific value
            + [Tag.for_agent_id(context.agent.id).id]  # Agent-specific value
            + [ContextVariableStore.GLOBAL_KEY]  # Global value
        )
```

No other changes to `_load_context_variables`, `_load_context_variable_value`, or `load_fresh_context_variable_value` — they are key-agnostic and already handle tool-based fallback per-key.

- [ ] **Step 5: Run the test and confirm it passes**

Run: `uv run pytest tests/sdk/test_variables.py::Test_that_variable_value_for_agent_is_used_when_no_customer_or_tag_value_exists -v`

Expected: PASS.

- [ ] **Step 6: Format and commit**

```bash
uv run ruff format src/parlant/core/engines/alpha/engine.py tests/sdk/test_variables.py
git add src/parlant/core/engines/alpha/engine.py tests/sdk/test_variables.py
git commit -m "$(cat <<'EOF'
Add agent tier to context-variable value resolution

The engine now resolves context variable values in the order
customer -> customer tag -> agent (by id) -> global -> tool-based.
The agent tier reads from key Tag.for_agent_id(agent.id).id in the
existing values store; no schema change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Regression guard — customer-tag still beats agent

**Files:**
- Test: `tests/sdk/test_variables.py` (append a new class)

**Why this test:** It locks in the precedence slot we chose in Task 2. If a future change accidentally swaps the order so the agent tier sits above the customer-tag tier, this test fails. The test is expected to pass immediately after Task 2 — it does not have its own implementation phase. (No production code is modified in this task.)

- [ ] **Step 1: Append the regression-guard test to `tests/sdk/test_variables.py`**

Append at the end of the file:

```python
class Test_that_customer_tag_value_takes_precedence_over_agent_value(SDKTest):
    async def setup(self, server: p.Server) -> None:
        self.agent = await server.create_agent(
            name="Var Agent",
            description="Agent for precedence regression test",
        )

        self.tag = await server.create_tag("premium_users")

        self.customer = await server.create_customer(
            "Jane Doe",
            tags=[self.tag.id],
        )

        self.variable = await self.agent.create_variable(
            name="subscription_plan",
            description="The current subscription plan of the user.",
        )

        await self.variable.set_global_value("free")
        await self.variable.set_value_for_agent(self.agent, "agent_default")
        await self.variable.set_value_for_tag(self.tag.id, "tag_value")

        self.retrieved_value: p.JSONSerializable | None = None

        variable = self.variable

        async def custom_retriever(ctx: p.RetrieverContext) -> p.RetrieverResult:
            self.retrieved_value = await variable.get_value()
            return p.RetrieverResult(data={"plan": self.retrieved_value})

        await self.agent.attach_retriever(custom_retriever)

    async def run(self, ctx: Context) -> None:
        await ctx.send_and_receive_message(
            customer_message="What is my plan?",
            recipient=self.agent,
            sender=self.customer,
        )

        assert self.retrieved_value == "tag_value", (
            f"Expected customer-tag value 'tag_value' (must beat agent tier), "
            f"got {self.retrieved_value!r}"
        )
```

- [ ] **Step 2: Run the test and confirm it passes**

Run: `uv run pytest tests/sdk/test_variables.py::Test_that_customer_tag_value_takes_precedence_over_agent_value -v`

Expected: PASS — the engine's precedence list (after Task 2) still places `[f"tag:{tag_id}" for tag_id in context.customer.tags]` before `Tag.for_agent_id(context.agent.id).id`, so the customer-tag value `"tag_value"` is selected first.

- [ ] **Step 3: Run the full file to confirm all variable tests still pass**

Run: `uv run pytest tests/sdk/test_variables.py -v`

Expected: every existing test still passes plus the three new tests pass.

- [ ] **Step 4: Format and commit**

```bash
uv run ruff format tests/sdk/test_variables.py
git add tests/sdk/test_variables.py
git commit -m "$(cat <<'EOF'
Add regression guard: customer-tag value beats agent value

Locks in the precedence slot of the new agent tier (between
customer-tag and global). Fails if a future change swaps the order.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Lint and final verification

**Files:** none modified beyond what previous tasks may need to touch up.

**Why:** Project policy (`CLAUDE.md`) is to run `uv run python scripts/lint.py --mypy --ruff` after changes and to format with ruff. This task verifies the whole change passes both type-check and lint, and that the targeted tests still pass together.

- [ ] **Step 1: Run the lint script**

Run: `uv run python scripts/lint.py --mypy --ruff`

Expected: exits 0 with no mypy or ruff errors. If errors appear, fix them inline (most likely: a missing type annotation on the new `Variable` methods — they should already be fully annotated per the spec, but verify). Re-run until clean.

- [ ] **Step 2: Run the full variables test file once more**

Run: `uv run pytest tests/sdk/test_variables.py -v`

Expected: all tests pass (existing + the three new ones).

- [ ] **Step 3: If Step 1 required fixes, format and commit them**

If no fixes were needed, skip this step.

```bash
uv run ruff format <files-touched>
git add <files-touched>
git commit -m "$(cat <<'EOF'
Fix lint findings for agent-scoped context variable values

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-review notes

- **Spec coverage:** Section 1 of the spec (no store changes) → reflected in zero store tasks. Section 2 (engine change) → Task 2. Section 3 (SDK changes) → Task 1. Section 4 (three tests) → Tasks 1, 2, 3 (one test each). Lint/format obligations from `CLAUDE.md` → Task 4.
- **Type consistency:** `Tag.for_agent_id(agent_id).id` is used identically in both engine.py (Task 2) and sdk.py (Task 1, via the `_Tag` alias already imported at `sdk.py:272`). Method names `set_value_for_agent` / `get_value_for_agent` are consistent across plan, spec, and tests.
- **No placeholders:** Every code block is final. Every command is concrete. No "TBD", "TODO", or "fill in" remains.
