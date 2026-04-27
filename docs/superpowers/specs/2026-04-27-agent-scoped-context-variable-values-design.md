# Agent-scoped context variable values

## Problem

The engine currently resolves a context variable's value for a given turn by trying keys in this order, stopping at the first match:

1. `customer.id` — customer-specific value
2. `f"tag:{tag_id}"` for each customer tag — customer-tag value
3. `ContextVariableStore.GLOBAL_KEY` (`"DEFAULT"`) — global value
4. (Variable's tool, if defined) — tool-based fallback inside `_load_context_variable_value`

Reference: `src/parlant/core/engines/alpha/engine.py:1101-1105`.

There is no tier for "value owned by *this agent*, applied to every customer this agent talks to, regardless of customer tags". Agents that share a single context variable definition currently can't carry different defaults without either tagging every customer or defining separate variables per agent.

## Goal

Insert an **agent tier** between the customer-tag tier and the global tier. New precedence:

1. Customer-specific
2. Customer-tag
3. **Agent (by id)** — new
4. Global
5. Tool-based

Expose this tier in the SDK via `Variable.set_value_for_agent(agent, value)` and `Variable.get_value_for_agent(agent)`.

## Non-goals

- No agent-tag tier (e.g., "all agents tagged X share this default"). YAGNI; can be added later if needed.
- No new method on `Agent` (e.g., `agent.set_variable_value(variable, value)`). Surface stays on `Variable`, mirroring the existing `set_value_for_customer` / `set_value_for_tag` / `set_global_value` pattern.
- No store schema change, no migration. The values store remains key-agnostic.

## Design

### Key encoding

Reuse the existing `Tag.for_agent_id(agent_id)` helper from `src/parlant/core/tags.py` (line 53–59). Its `.id` produces the canonical string `f"agent:{agent_id}"`. Use that string directly as the value-store key for the agent tier.

This is distinct from the customer-tag tier: customer tags are wrapped as `f"tag:{tag_id}"` before being used as keys (`engine.py:1103`), so a customer carrying a tag named `agent:X` would produce key `"tag:agent:X"` — different from the agent-tier key `"agent:X"`. No collision, no aliasing across tiers.

The `agent:{id}` namespace is already used across the codebase as a *resource ownership* tag (e.g., a context variable is tagged `agent:{id}` to scope it to that agent — see `entity_cq.py:124,214,262`). Using the same string as a *value key* in the values store is a parallel use of the same namespace; the values store itself is unaffected because it is key-agnostic.

### Store changes

None. `ContextVariableStore.update_value` / `read_value` / `delete_value` / `list_values` already accept arbitrary string keys. No new helper, no new method, no schema or migration change.

### Engine changes

**File:** `src/parlant/core/engines/alpha/engine.py`

Modify `_load_context_variables` (around line 1089) so the precedence list includes the agent tier:

```python
keys_to_check_in_order_of_importance = (
    [context.customer.id]
    + [f"tag:{tag_id}" for tag_id in context.customer.tags]
    + [Tag.for_agent_id(context.agent.id).id]   # NEW: agent-specific value
    + [ContextVariableStore.GLOBAL_KEY]
)
```

Add `from parlant.core.tags import Tag` to the imports — `engine.py` does not currently import `Tag`.

`_load_context_variable_value` and `load_fresh_context_variable_value` (around line 2023 and 2200) require no changes — they already accept any key and run the variable's tool (if any) against it for the tool-based fallback.

### SDK changes

**File:** `src/parlant/sdk.py`

Add two methods to the `Variable` dataclass alongside the existing setters/getters (around line 2942–2996):

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

`Tag` is already imported as `_Tag` for SDK use elsewhere in the file (e.g., `sdk.py:3265`), so no new imports.

No changes to existing `set_value_for_customer` / `set_value_for_tag` / `set_global_value` / `get_value*` siblings. No new method on `Agent`.

## Testing

**File:** `tests/sdk/test_variables.py`

Three new `SDKTest` classes following the existing patterns in the same file.

### Test 1 — SDK round-trip for agent-scoped value

`Test_that_a_variable_value_can_be_set_for_an_agent` — set a value via `set_value_for_agent`, read it back via `get_value_for_agent`, assert equality. Mirrors `Test_that_a_variable_value_can_be_set_for_a_customer` (line 71). Proves the SDK methods round-trip through the store with the right key encoding.

### Test 2 — Engine resolves agent tier when no customer/tag value exists

`Test_that_variable_value_for_agent_is_used_when_no_customer_or_tag_value_exists` — set both a global value (`"free"`) and an agent value (`"premium"`), then run a turn with a custom retriever that calls `variable.get_value()`. Assert the retriever observed `"premium"`. Mirrors `Test_that_variable_get_value_returns_correct_value_when_called_from_retriever` (line 180). Proves the engine actually reads the new tier and that it beats global.

### Test 3 — Customer-tag still beats agent (regression guard)

`Test_that_customer_tag_value_takes_precedence_over_agent_value` — set a global value, an agent value, and a customer-tag value; the customer carries the tag. Assert the retriever observes the customer-tag value. Proves the new tier was inserted in the *correct slot* (between customer-tag and global), not above customer-tag.

### Why each test fails before implementation

- Test 1: `Variable` has no `set_value_for_agent` / `get_value_for_agent` methods.
- Test 2: engine never reads `f"agent:{agent.id}"` keys, so `variable.get_value()` falls through to the global value `"free"`.
- Test 3: passes today (customer-tag already beats global). It's a regression guard against accidentally inserting the agent tier above customer-tag during implementation.

### Tests deliberately not added

- Customer-specific vs agent precedence: customer-specific already beats every tier below it and is well covered by existing customer/global tests. Adding another would duplicate.
- Tool-based fallback interaction: tool-based fallback is per-key inside `_load_context_variable_value` and is unaffected by adding a new key tier; existing coverage in core tests is sufficient.

## Files touched

- `src/parlant/core/engines/alpha/engine.py` — one-line precedence-list change + import.
- `src/parlant/sdk.py` — two new methods on `Variable`.
- `tests/sdk/test_variables.py` — three new `SDKTest` classes.

No store, schema, migration, or API-layer changes.
