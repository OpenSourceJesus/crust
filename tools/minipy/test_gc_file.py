# A file object must survive a collection while it is still referenced.
#
# `open()` returns a container (tag 16) like a list or a dict, so its heap slot
# is reclaimable -- but _gc_mark only traced tags 7..12, 14 and 15, so the slot
# was swept while the guest still held the handle and the next allocation reused
# it. The read then returned whatever had landed there (an integer, in the first
# reproduction of this) instead of the file's contents.
#
# The loop exists only to push the heap past GC_MIN_HEAP so a collection
# actually happens between the open and the read; without it the test passes
# either way.
import os

PATH = "/tmp/minipy_test_gc_file.txt"

with open(PATH, "w") as fh:
    fh.write("alpha\n")
    fh.write("beta\n")

held = open(PATH)

i = 0
total = 0
while i < 20000:
    row = [i, i + 1]
    total = total + row[0]
    i = i + 1

print(held.read())
held.close()
print("total=" + str(total))
print("exists=" + str(os.path.exists(PATH)))
