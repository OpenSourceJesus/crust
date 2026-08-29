# Container churn: many short-lived lists and dicts, tiny live set. This is
# the workload the heap collector exists for -- every other benchmark here
# allocates scalars and strings, which the escape-analysis / value-freelist
# work already handles, so without this one the collector's effect is
# invisible to the harness.
total = 0
i = 0
while i < 300000:
    xs = [i, i + 1, i + 2]
    d = {"a": xs[0], "b": xs[2]}
    total = total + d["a"] + d["b"]
    i = i + 1
print(total)
