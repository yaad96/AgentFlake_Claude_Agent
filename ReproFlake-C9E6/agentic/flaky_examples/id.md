# ID (Implementation-Dependent) Flaky Test — Repair Exemplar

## What ID means

The test depends on an implementation-defined ordering of an unordered
data structure — typically `HashMap`, `HashSet`, `Iterator`, or any API
whose spec says "no order is guaranteed" but whose default JDK behaviour
happens to be stable enough for the test to "usually" pass. NonDex
shuffles those iteration orders deterministically and the test fails on
some seeds.

## How to diagnose

1. Read the victim with `get_test_code`. Look for:
   - `.iterator()`, `.entrySet()`, `.keySet()`, `.values()`, `.toArray()`
     calls on Maps/Sets where the test then indexes by position
   - `.get(0)`/`.get(N)` on a collection materialised from a Set
   - Assertions that compare result lists element-by-element when the
     producer is allowed to emit in any order
   - String equality on `Map.toString()` / `Set.toString()` output
2. The flakyDoctor heuristic: search the failing class for method names
   matching `iterator|sort|order|first|last|min|max|stream` — these
   often sit at the failure boundary.
3. `get_code` on production methods named in the stack trace will reveal
   the upstream collection type. If it's a `HashMap`, you've found it.

## Fix strategies (pick the smallest)

- **Replace the unordered collection with an ordered one** in the test
  — `LinkedHashMap`, `TreeMap`, `LinkedHashSet`, `TreeSet`. Only when
  the test owns the collection.
- **Sort the result before asserting** — `Collections.sort(result)`, or
  use `containsInAnyOrder(...)` matcher, or `assertEquals(set1, set2)`
  on sets rather than lists.
- **Compare as multiset** — `assertEquals(Set.of(...), result)` instead
  of `assertEquals(List.of(...), result)`.
- **Fix the assertion**, not the production code — the production
  contract is "any order"; the bug is that the test assumed a specific
  one.

## Worked example

```java
// Before
Map<String,Integer> m = service.histogram();
List<String> keys = new ArrayList<>(m.keySet());
assertEquals("a", keys.get(0));  // depends on HashMap iteration order

// After
Map<String,Integer> m = service.histogram();
List<String> keys = new ArrayList<>(m.keySet());
Collections.sort(keys);
assertEquals("a", keys.get(0));
```

If the production code emits a `HashMap` and the test reasonably expects
ordering, prefer making the test order-tolerant; only switch the
production map type if the API contract clearly demands ordering.
