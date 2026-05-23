# TD (Timing-Dependent) Flaky Test — Repair Exemplar

## What TD means

The test fails non-deterministically because its outcome depends on
timing, scheduling, or other sources of non-determinism (clock readings,
randomness, async completion, network round-trip jitter). Unlike OD,
there is no separate polluter test — the test pollutes itself across
invocations or relies on a race that sometimes loses.

## How to diagnose

1. Read the victim source with `get_test_code` and look for:
   - `Thread.sleep(N)`, hard-coded waits, polling loops without bounded retries
   - `System.currentTimeMillis()`, `LocalDateTime.now()`, `new Random()`
   - Async tasks (`CompletableFuture`, `ExecutorService.submit`, network
     calls) whose completion is assumed without joining
   - Assertions on counts/state that depend on the order tasks resolve
2. Pull the failure with `get_error_logs('test_failure')`. The exception
   line + the assertion message often name the racy quantity directly
   (e.g., "expected 5 events but got 4").
3. `get_rv_trace_diff` is sometimes empty for pure-data races but can
   highlight collection ops that happen with different frequencies
   between passing and failing runs.

## Fix strategies (pick the smallest)

- **Replace clock/random with deterministic source** — inject a fixed
  `Clock`, set a seed on `Random`, mock the time source.
- **Replace `Thread.sleep` with synchronization** — `CountDownLatch`,
  `await()`, `Future.get(timeout)`. Convert "wait long enough" into
  "wait for the actual signal".
- **Join asynchronous work explicitly** — `executor.shutdown(); awaitTermination(...)`,
  `future.get()`, `CompletableFuture.allOf(...).join()` before asserting.
- **Bound the polling loop** — use Awaitility's `until(...)` with a real
  timeout if synchronization isn't available; raise the timeout to a
  safely large value but keep it bounded.

## Worked example

The test schedules 5 async tasks then asserts `counter.get() == 5`. The
last task can land after the assertion under load.

```java
// Before
executor.submit(task);
assertEquals(5, counter.get());

// After
executor.submit(task);
executor.shutdown();
assertTrue(executor.awaitTermination(10, SECONDS));
assertEquals(5, counter.get());
```

Keep the change minimal. Do not adjust the assertion value to mask a
real bug.
