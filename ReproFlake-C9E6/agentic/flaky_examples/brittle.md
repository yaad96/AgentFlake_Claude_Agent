# Brittle (Order-Dependent variant) Flaky Test — Repair Exemplar

## What Brittle means

A Brittle test is structurally identical to an OD test: a polluter test runs
immediately before the victim and leaves shared state dirty, causing the victim
to fail. The only difference from OD is that the root cause is typically more
subtle — often a singleton reset, a cached configuration, or a lazily-initialized
resource that the victim assumes is in a default state.

The reproduction command runs polluter immediately before victim in the same JVM.

## How to diagnose

1. Read both POLLUTER and VICTIM source via `get_test_code`.
2. Identify what shared mutable state the polluter sets and the victim reads.
   Common culprits in brittle tests:
   - Singleton services with internal caches (e.g., a configuration registry)
   - Static thread-local state not reset in `@After`
   - JVM-level properties (`System.setProperty`) set without cleanup
   - Lazy-initialised resources (connection pools, executor services) that survive
     between test classes when the polluter shuts them down non-cleanly
3. Use `get_code` to inspect production classes that the victim instantiates or
   calls — the pollution often lives in production code, not the test itself.
4. Call `get_rv_trace_diff` if you need runtime evidence of which spec was violated.

## Fix strategies

- **Add `@After` / `@AfterEach` cleanup to the polluter** — restore whatever global
  state the polluter modifies so the victim starts clean. This is the preferred fix
  when the polluter genuinely owns the state.
- **Add `@Before` / `@BeforeEach` setup to the victim** — initialise the state the
  victim depends on rather than trusting the JVM default. Prefer this when multiple
  polluters could precede the victim.
- **Reset a singleton/registry** — if a production-level singleton is contaminated,
  add a reset method (or call an existing one) in the appropriate lifecycle hook.

## Worked example

Polluter registers a listener on a static `EventBus` singleton:
```java
EventBus.getInstance().register(myListener);
```
The victim later expects zero listeners and fails. The polluter has no `@After`
that deregisters. Fix: add cleanup to the polluter.

```java
@After
public void cleanup() {
    EventBus.getInstance().unregister(myListener);
}
```

Keep changes minimal. Do not rename methods, change assertions, or reformat
surrounding code.
