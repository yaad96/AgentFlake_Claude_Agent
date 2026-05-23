# OD (Order-Dependent) Flaky Test — Repair Exemplar

## What OD means

An Order-Dependent test passes when run alone but fails when a "polluter"
test runs immediately before it within the same JVM. The polluter mutates
shared mutable state (static field, singleton, registry, system property,
file, cache, etc.); the victim assumes the un-polluted state.

## How to diagnose

1. Read both `POLLUTER` and `VICTIM` source from `get_test_code`.
2. Identify the *shared state* the polluter mutates and the victim reads.
   Common shapes:
     - `static` field set by the polluter, read by the victim
     - Singleton's internal state (`LoggerFactory.getLogger`, cached
       configuration, registered listeners)
     - System property set without `@After` cleanup
     - File written to a fixed path without per-test isolation
3. Look at the production code on the failure path (`get_code` with the
   classes named in the stack trace). Many OD bugs sit in production
   code that *trusts* a piece of shared state to be in a clean
   initial shape.
4. If the source alone doesn't pin down the pollution site, call
   `get_rv_trace_diff` — TraceMOP often reports the exact spec
   (`Map_UnsafeIterator`, `Collection_Unsynchronized`, etc.) that names
   which contract is being violated.

## Fix strategies (pick the smallest)

- **Cleanup in the polluter** — `@After`/`@AfterEach` that restores the
  shared state to its default (`Foo.setBar(null)`, `cache.clear()`,
  `System.clearProperty("x")`). Best when the polluter genuinely mutates
  state it owns.
- **Setup in the victim** — `@Before`/`@BeforeEach` that initializes the
  state the victim depends on, instead of relying on the JVM-fresh
  default. Best when the victim is the late-comer and many polluters
  could plausibly precede it.
- **Defensive check in production** — null-guard or lazy-init in the
  production class that's reading the polluted slot. Use this sparingly;
  it's the right answer only when the production API genuinely permits
  the polluted shape and the test is asserting incorrectly that it
  doesn't.

## Worked example

Victim assumes `LoggerFactory.getLogger("X")` returns a logger whose
internal `level` field is null (the JVM-default). Polluter calls
`logger.setLevel(Level.DEBUG)` and never resets it. Fix: add an
`@AfterEach` to the polluter that resets the level.

```java
// Polluter test class:
@AfterEach
void resetLogLevel() {
    LoggerFactory.getLogger("X").setLevel(null);
}
```

The change must be minimal. Do not rename methods, change assertions, or
reformat surrounding code.
