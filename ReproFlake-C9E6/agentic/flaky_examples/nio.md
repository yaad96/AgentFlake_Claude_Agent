# NIO (Non-Idempotent-Outcome) Flaky Test — Repair Exemplar

## What NIO means

The test passes when run alone but fails when run a second time in the
same JVM, because it pollutes shared static state that its own second
invocation then reads. The test IS its own polluter — across
invocations of itself within one process.

Definition: Wei et al., ICSE 2022 — "Preempting Flaky Tests via
Non-Idempotent-Outcome Tests".

## How to diagnose

1. Read the victim with `get_test_code` and look for writes to:
   - `static` fields (counters, lists, maps, caches, registry instances)
   - Singletons reached via `getInstance()` / `INSTANCE`
   - System properties / environment-backed configuration
   - Files at a fixed path
2. Trace the read side: `get_code` the production methods the test
   calls; check if any of them maintain in-process state across calls.
3. `get_rv_trace_diff` is highest-leverage for NIO when the bug is
   driven by control-flow pollution (e.g., `static List` accumulation
   shows up as different `Collection.add`/`.clear` event counts). It is
   often empty for primitive pollution (`static int counter`); in that
   case fall back to source review.

## Fix strategies (pick the smallest)

- **Reset polluted state at the end of the test method** — the
  canonical NIO fix. Add an explicit `Foo.iterations = 0;` or
  `Foo.cache.clear();` as the last line of the test (or in an
  `@After`/`@AfterEach` if the test class lacks one).
- **Reset in `@Before`/`@BeforeEach`** when the polluted slot lives on
  a singleton and "reset at the start" reads more naturally than "reset
  at the end".
- **Stop using static state for what should be per-test state** — if
  the polluted field is owned by test infrastructure, move it to a
  per-test instance field. Use this only when the existing API already
  permits it.

## Worked example

```java
public class FooTest {
    @Test
    public void test() {
        Foo.iterations++;
        // ... assertions assuming iterations starts at 1 ...
        Foo.iterations = 0;   // ADDED: reset for next invocation in same JVM
    }
}
```

Or, equivalently:

```java
@After
public void resetFooState() {
    Foo.iterations = 0;
}
```

Keep the change minimal — typically one line of reset code. Do NOT
change the test's assertions or modify how the production class tracks
its state unless the static field is clearly a design defect.
