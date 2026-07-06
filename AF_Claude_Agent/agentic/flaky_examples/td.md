# TD (Timing-Dependent) Flaky Test — Repair Exemplar

## What TD means

The test fails non-deterministically because its outcome depends on
timing, scheduling, or other sources of non-determinism (clock readings,
randomness, async completion, network round-trip jitter). For async/concurrency
cases, the preferred repair is to wait for the actual completion signal or
state transition, not merely to wait longer.

## How to diagnose

1. Read the victim source with `get_test_code` and look for:

   * `Thread.sleep(N)`, hard-coded waits, polling loops without bounded retries
   * `System.currentTimeMillis()`, `LocalDateTime.now()`, `new Random()`
   * Async tasks (`CompletableFuture`, `ExecutorService.submit`, network
     calls) whose completion is assumed without joining
   * Assertions on counts/state that depend on the order tasks resolve
2. Pull the failure with `get_error_logs('test_failure')`. The exception
   line + the assertion message often name the racy quantity directly
   (e.g., "expected 5 events but got 4").
3. `get_rv_trace_diff` is sometimes empty for pure-data races but can
   highlight collection ops that happen with different frequencies
   between passing and failing runs.

## Fix strategies (pick the smallest)

Prefer a real synchronization repair:

* Replace nondeterministic clock/random use with deterministic sources.
* Replace `Thread.sleep` or unjoined async work with `CountDownLatch`,
  `Future.get(timeout)`, `CompletableFuture.allOf(...).join()`, or
  `executor.awaitTermination(...)`.
* For polling, wait for a concrete condition/state transition using the
  project’s existing wait helper or Awaitility.
* Before changing a wait, inspect the code that updates the state, metric,
  or asserted value being waited for.
* Prefer a minimal synchronization point near that update code, such as a
  completion signal, future, latch, callback, thread termination, initialized
  object, or stable state.
* Do not submit a patch that only changes wait/sleep/timeout constants.

## Worked example

The test schedules 5 async tasks then asserts `counter.get() == 5`. The
last task can land after the assertion under load.

```java
// Before
executor.submit(task);
assertEquals(5, counter.get());

// After
Future<?> f = executor.submit(task);
f.get(10, SECONDS);
assertEquals(5, counter.get());
```

Keep the change minimal. Do not adjust the assertion value to mask a
real bug. Do not submit a timeout-only repair; first use the code that
updates the waited/asserted state to find a minimal synchronization-based
repair.
