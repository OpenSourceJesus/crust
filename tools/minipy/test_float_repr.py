"""Float repr and round-tripping.

Run three ways (CPython, the reference VM, the py2c-compiled interpreter) and
required to print identically. Two bugs motivated it, both of which would have
corrupted a self-hosted compile rather than announcing themselves:

  * `str(float)` in the compiled runtime was `sprintf("%g", ...)` -- six
    significant digits. 3.141592653589793 printed as 3.14159, and
    1.0000000000000002 printed as 1. When the compiler being run is the one
    writing float constants into generated code, that is a value change.

  * `float(str)` in the interpreter scaled the exponent with a loop of
    `p = p * 10.0`, accumulating rounding error across 308 iterations, so
    1e308 came back as 9.999999999999998e+307.

Together they are the repr/parse round trip, which is the property `minipy
py2c.py x.py` needs in order to produce byte-identical C to CPython.

The values below are chosen to sit on the boundaries: where CPython switches
between fixed and exponential notation (1e15/1e16, 1e-4/1e-5), where 17
significant digits are needed (1.0000000000000002), at the extremes of the
double range including a denormal, and at negative zero, whose sign is easy to
drop when an integral value is shortcut through int().
"""

VALUES = [
    0.0, -0.0, 1.0, -1.0, 0.5, 0.1,
    3.141592653589793,
    1.0000000000000002,
    0.3333333333333333,
    15000000000.0,
    123456789012345.0,
    1234567.0,
    100000.0,
    1e15, 1e16, 1e17,
    1e-4, 1e-5,
    2.5e-10,
    1e22,
    1e308, 1e-308,
    2.2250738585072014e-308,
    1e-323,
]

print("-- repr --")
for v in VALUES:
    print(str(v))

print("-- round trip --")
for v in VALUES:
    # repr then parse must land on the identical double. `==` is the check that
    # matters; printing it keeps the three-way comparison honest about which
    # value failed rather than just how many.
    back = float(str(v))
    print(str(back) + " " + str(back == v))

print("-- literal forms --")
print(str(1e308))
print(str(.5))
print(str(10.))
print(str(1.5e-3))
print(str(1_000.5))
print(str(0x1f) + " " + str(0b1010) + " " + str(0o777) + " " + str(1_000))
print(str(0xdead_beef))

print("-- arithmetic keeps precision --")
acc = 0.0
i = 0
while i < 10:
    acc = acc + 0.1
    i = i + 1
print(str(acc))
print(str(1.0 / 3.0))
print(str(2.0 * 3.0 / 7.0))
# NB: `2.0 ** 0.5` is deliberately absent. v_pow() in interp.py returns 0.0 for
# a float or negative exponent -- an explicit v0 stub, not a regression. It
# does not block self-hosting: nothing in py2c.py, rasm.py, rasm_obj.py,
# rlink.py or rast.py uses ** with anything but a non-negative integer.
