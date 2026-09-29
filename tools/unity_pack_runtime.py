"""The packed engine's runtime for common .NET / Unity static APIs.

A table, not a pass per API: each entry maps a C# spelling -- `Mathf.Sqrt`,
`Random.Range`, `int.Parse`, `Path.Combine`, `File.ReadAllText` -- to an
engine helper, the C for it (in the C++ subset cpprust lowers), and what it
returns, so the typed concatenation and `Debug.Log` format the result as
they format anything else. unity_pack lowers a method body through
`lower_runtime_apis` early, while the text is still C#, records the helpers
used, and emits only those (with the ones they call) where it emits the
string helpers.

What each one does is .NET's or Unity's, where C allows; the differences
are documented in UNITY_PACK.md (*Runtime*):

* `float.Parse` reads the invariant culture (`.` decimal point), not the
  current one;
* `Random` is a seeded xorshift32 (Unity seeds from the clock), so a run
  repeats unless the script calls `Random.InitState`;
* `Time.realtimeSinceStartup` / `unscaledTime` are `Time.time`: the packed
  engine has no time scale, and reads no wall clock;
* a `Directory` or `File` call on a path that does not exist, or input
  `Parse` rejects, aborts with the .NET exception's name, as an unhandled
  exception ends the process.
"""
import re

# ---------------------------------------------------------------------------
# The helpers: name -> (C, result kind, helpers it calls, needs <math.h>)
# Result kinds: "float", "int", "bool", "string", "strarray", "void".
# ---------------------------------------------------------------------------

_M = {}


def _h(name, kind, c, deps=(), math=False):
    _M[name] = (c, kind, tuple(deps), math)


# -- Mathf ------------------------------------------------------------------

for _n, _f in (("Sqrt", "sqrt"), ("Floor", "floor"), ("Ceil", "ceil"),
               ("Tan", "tan"), ("Asin", "asin"), ("Acos", "acos"),
               ("Atan", "atan"), ("Exp", "exp"), ("Log", "log"),
               ("Log10", "log10")):
    _h("Mathf_" + _n, "float",
       "static float Mathf_%s(float f) { return (float)%s((double)f); }"
       % (_n, _f), math=True)
# Unity's Round is .NET's: halves go to the even neighbour (rint's default).
_h("Mathf_Round", "float",
   "static float Mathf_Round(float f) { return (float)rint((double)f); }",
   math=True)
_h("Mathf_FloorToInt", "int",
   "static int Mathf_FloorToInt(float f) { return (int)floor((double)f); }",
   math=True)
_h("Mathf_CeilToInt", "int",
   "static int Mathf_CeilToInt(float f) { return (int)ceil((double)f); }",
   math=True)
_h("Mathf_RoundToInt", "int",
   "static int Mathf_RoundToInt(float f) { return (int)rint((double)f); }",
   math=True)
_h("Mathf_Pow", "float",
   "static float Mathf_Pow(float f, float p) {\n"
   "    return (float)pow((double)f, (double)p);\n}", math=True)
_h("Mathf_Atan2", "float",
   "static float Mathf_Atan2(float y, float x) {\n"
   "    return (float)atan2((double)y, (double)x);\n}", math=True)
_h("Mathf_LogB", "float",
   "static float Mathf_LogB(float f, float p) {\n"
   "    return (float)(log((double)f) / log((double)p));\n}", math=True)
_h("Mathf_Clamp01", "float",
   "static float Mathf_Clamp01(float v) {\n"
   "    if (v < 0.f) return 0.f;\n    if (v > 1.f) return 1.f;\n"
   "    return v;\n}")
_h("Mathf_InverseLerp", "float",
   "static float Mathf_InverseLerp(float a, float b, float v) {\n"
   "    if (a == b) return 0.f;\n"
   "    return Mathf_Clamp01((v - a) / (b - a));\n}",
   deps=("Mathf_Clamp01",))
_h("Mathf_LerpUnclamped", "float",
   "static float Mathf_LerpUnclamped(float a, float b, float t) {\n"
   "    return a + (b - a) * t;\n}")
_h("Mathf_MoveTowards", "float",
   "static float Mathf_MoveTowards(float c, float t, float d) {\n"
   "    float diff = t - c;\n"
   "    if ((diff < 0.f ? -diff : diff) <= d) return t;\n"
   "    return c + (diff < 0.f ? -d : d);\n}")
_h("Mathf_Repeat", "float",
   "static float Mathf_Repeat(float t, float len) {\n"
   "    float r = t - (float)floor((double)(t / len)) * len;\n"
   "    if (r < 0.f) r = 0.f;\n    if (r > len) r = len;\n    return r;\n}",
   math=True)
_h("Mathf_PingPong", "float",
   "static float Mathf_PingPong(float t, float len) {\n"
   "    float r = Mathf_Repeat(t, len * 2.f) - len;\n"
   "    return len - (r < 0.f ? -r : r);\n}", deps=("Mathf_Repeat",))
_h("Mathf_DeltaAngle", "float",
   "static float Mathf_DeltaAngle(float c, float t) {\n"
   "    float d = Mathf_Repeat(t - c, 360.f);\n"
   "    if (d > 180.f) d = d - 360.f;\n    return d;\n}",
   deps=("Mathf_Repeat",))
_h("Mathf_SmoothStep", "float",
   "static float Mathf_SmoothStep(float a, float b, float t) {\n"
   "    t = Mathf_Clamp01(t);\n"
   "    t = -2.f * t * t * t + 3.f * t * t;\n"
   "    return b * t + a * (1.f - t);\n}", deps=("Mathf_Clamp01",))
_h("Mathf_Approximately", "bool",
   "static int Mathf_Approximately(float a, float b) {\n"
   "    float d = b - a;\n"
   "    float m = (a < 0.f ? -a : a) > (b < 0.f ? -b : b)\n"
   "        ? (a < 0.f ? -a : a) : (b < 0.f ? -b : b);\n"
   "    float tol = 1e-6f * m;\n"
   "    if (tol < 1.121039e-44f) tol = 1.121039e-44f;\n"
   "    return (d < 0.f ? -d : d) < tol;\n}")
_h("Mathf_Infinity", "float",
   "static float Mathf_Infinity(void) {\n"
   "    float z = 0.f;\n    return 1.f / z;\n}")

#: Mathf constants, as C float literals (or a call).
MATHF_CONSTANTS = {
    "PI": "3.14159274f",
    "Deg2Rad": "0.0174532924f",
    "Rad2Deg": "57.2957802f",
    "Epsilon": "1.401298e-45f",
    "Infinity": "Mathf_Infinity()",
    "NegativeInfinity": "(-Mathf_Infinity())",
}

# -- Random (UnityEngine.Random) -------------------------------------------

_h("_engine_rng", "void",
   "/* UnityEngine.Random: xorshift32, seeded (Random.InitState reseeds) */\n"
   "static unsigned _engine_rng_state = 2463534242u;\n"
   "static unsigned _engine_rng_next(void) {\n"
   "    unsigned x = _engine_rng_state;\n"
   "    x = x ^ (x << 13);\n    x = x ^ (x >> 17);\n    x = x ^ (x << 5);\n"
   "    _engine_rng_state = x;\n    return x;\n}")
_h("Random_value", "float",
   "static float Random_value(void) {\n"
   "    return (float)(_engine_rng_next() >> 8) / 16777215.f;\n}",
   deps=("_engine_rng",))
_h("Random_Range_f", "float",
   "static float Random_Range_f(float a, float b) {\n"
   "    return a + (b - a) * Random_value();\n}", deps=("Random_value",))
# int: max exclusive; Unity returns min when max <= min.
_h("Random_Range_i", "int",
   "static int Random_Range_i(int a, int b) {\n"
   "    if (b <= a) return a;\n"
   "    return a + (int)(_engine_rng_next() % (unsigned)(b - a));\n}",
   deps=("_engine_rng",))
_h("Random_InitState", "void",
   "static void Random_InitState(int seed) {\n"
   "    _engine_rng_state = seed ? (unsigned)seed : 1u;\n}",
   deps=("_engine_rng",))

# -- int / float Parse ------------------------------------------------------

_h("_cs_parse_trim", "void",
   "static int _cs_is_space(char c) { return c == ' ' || (c >= 9 && c <= 13); }")
_h("_cs_int_TryParse", "bool",
   "static int _cs_int_TryParse(const char *s, int *out) {\n"
   "    char *end;\n    long v;\n"
   "    if (!s) return 0;\n"
   "    while (_cs_is_space(*s)) s = s + 1;\n"
   "    if (!*s) return 0;\n"
   "    v = strtol(s, &end, 10);\n"
   "    if (end == s) return 0;\n"
   "    while (_cs_is_space(*end)) end = end + 1;\n"
   "    if (*end) return 0;\n"
   "    if (v > 2147483647L || v < -2147483647L - 1L) return 0;\n"
   "    *out = (int)v;\n    return 1;\n}", deps=("_cs_parse_trim",))
# strtod is not in crust's <stdlib.h>: the invariant-culture decimal form
# read here -- sign, digits, `.` fraction, `e` exponent.
_h("_cs_read_decimal", "void",
   "static double _cs_read_decimal(const char *s, char **end) {\n"
   "    const char *p = s;\n"
   "    double v = 0.0;\n    double scale = 1.0;\n"
   "    int neg = 0;\n    int digits = 0;\n    int e = 0;\n    int eneg = 0;\n"
   "    if (*p == '+' || *p == '-') { neg = *p == '-'; p = p + 1; }\n"
   "    while (*p >= '0' && *p <= '9') {\n"
   "        v = v * 10.0 + (double)(*p - '0'); p = p + 1; digits = digits + 1;\n"
   "    }\n"
   "    if (*p == '.') {\n"
   "        p = p + 1;\n"
   "        while (*p >= '0' && *p <= '9') {\n"
   "            scale = scale / 10.0;\n"
   "            v = v + (double)(*p - '0') * scale;\n"
   "            p = p + 1; digits = digits + 1;\n"
   "        }\n"
   "    }\n"
   "    if (digits == 0) { *end = (char *)s; return 0.0; }\n"
   "    if (*p == 'e' || *p == 'E') {\n"
   "        const char *q = p + 1;\n"
   "        if (*q == '+' || *q == '-') { eneg = *q == '-'; q = q + 1; }\n"
   "        if (*q >= '0' && *q <= '9') {\n"
   "            while (*q >= '0' && *q <= '9') {\n"
   "                e = e * 10 + (*q - '0'); q = q + 1;\n"
   "            }\n"
   "            p = q;\n"
   "            while (e > 0) { v = eneg ? v / 10.0 : v * 10.0; e = e - 1; }\n"
   "        }\n"
   "    }\n"
   "    *end = (char *)p;\n"
   "    return neg ? -v : v;\n}")
_h("_cs_float_TryParse", "bool",
   "static int _cs_float_TryParse(const char *s, float *out) {\n"
   "    char *end;\n    double v;\n"
   "    if (!s) return 0;\n"
   "    while (_cs_is_space(*s)) s = s + 1;\n"
   "    if (!*s) return 0;\n"
   "    v = _cs_read_decimal(s, &end);\n"
   "    if (end == s) return 0;\n"
   "    while (_cs_is_space(*end)) end = end + 1;\n"
   "    if (*end) return 0;\n"
   "    *out = (float)v;\n    return 1;\n}", deps=("_cs_parse_trim", "_cs_read_decimal"))
_h("_cs_int_Parse", "int",
   "static int _cs_int_Parse(const char *s) {\n"
   "    int v = 0;\n"
   "    if (!_cs_int_TryParse(s, &v))\n"
   "        _cs_throw(\"FormatException: Input string was not in a correct "
   "format.\");\n"
   "    return v;\n}", deps=("_cs_int_TryParse",))
_h("_cs_float_Parse", "float",
   "static float _cs_float_Parse(const char *s) {\n"
   "    float v = 0.f;\n"
   "    if (!_cs_float_TryParse(s, &v))\n"
   "        _cs_throw(\"FormatException: Input string was not in a correct "
   "format.\");\n"
   "    return v;\n}", deps=("_cs_float_TryParse",))

# -- Path -------------------------------------------------------------------

_h("Path_Combine", "string",
   "static const char *Path_Combine(const char *a, const char *b) {\n"
   "    fastring t;\n"
   "    size_t n = strlen(a);\n"
   "    if (!*b) return _engine_str_keep(a, n);\n"
   "    if (b[0] == '/' || n == 0) return _engine_str_keep(b, strlen(b));\n"
   "    t.append_cstr(a);\n"
   "    if (a[n - 1] != '/') t.append_char('/');\n"
   "    t.append_cstr(b);\n"
   "    return _engine_str_keep(t.data(), t.size());\n}")
_h("Path_GetFileName", "string",
   "static const char *Path_GetFileName(const char *p) {\n"
   "    const char *s = strrchr(p, '/');\n"
   "    s = s ? s + 1 : p;\n"
   "    return _engine_str_keep(s, strlen(s));\n}")
_h("Path_GetExtension", "string",
   "static const char *Path_GetExtension(const char *p) {\n"
   "    const char *s = strrchr(p, '/');\n"
   "    const char *d;\n"
   "    s = s ? s + 1 : p;\n"
   "    d = strrchr(s, '.');\n"
   "    if (!d || d[1] == 0) return \"\";\n"
   "    return _engine_str_keep(d, strlen(d));\n}")
_h("Path_GetFileNameWithoutExtension", "string",
   "static const char *Path_GetFileNameWithoutExtension(const char *p) {\n"
   "    const char *s = strrchr(p, '/');\n"
   "    const char *d;\n"
   "    s = s ? s + 1 : p;\n"
   "    d = strrchr(s, '.');\n"
   "    return _engine_str_keep(s, d ? (size_t)(d - s) : strlen(s));\n}")
_h("Path_GetDirectoryName", "string",
   "static const char *Path_GetDirectoryName(const char *p) {\n"
   "    const char *s = strrchr(p, '/');\n"
   "    if (!s) return \"\";\n"
   "    if (s == p) return \"/\";\n"
   "    return _engine_str_keep(p, (size_t)(s - p));\n}")

# -- Directory / File -------------------------------------------------------

_h("_engine_fs", "void",
   "#ifndef CRUST_NO_POSIX_MKDIR\n#include <sys/stat.h>\n#endif\n"
   "/* 1 directory, 2 anything else there, 0 nothing. Without POSIX (the\n"
   "   crust front end), fopen: a directory opens for reading on Linux but\n"
   "   yields nothing, so it is told from a file that way. */\n"
   "static int _engine_fs_kind(const char *p) {\n"
   "#ifndef CRUST_NO_POSIX_MKDIR\n"
   "    struct stat st;\n"
   "    if (stat(p, &st) != 0) return 0;\n"
   "    return S_ISDIR(st.st_mode) ? 1 : 2;\n"
   "#else\n"
   "    FILE *f = fopen(p, \"r\");\n"
   "    int c;\n"
   "    if (!f) return 0;\n"
   "    c = fgetc(f);\n"
   "    if (c == -1 && ferror(f)) { fclose(f); return 1; }\n"
   "    fclose(f);\n"
   "    return 2;\n"
   "#endif\n}")
_h("Directory_Exists", "bool",
   "static int Directory_Exists(const char *p) {\n"
   "    return p && *p && _engine_fs_kind(p) == 1;\n}", deps=("_engine_fs",))
_h("Directory_CreateDirectory", "void",
   "static void Directory_CreateDirectory(const char *p) {\n"
   "    /* No class-type local in a preprocessor branch: cpprust places\n"
   "       its destructor at the function's end, outside the branch. */\n"
   "#ifndef CRUST_NO_POSIX_MKDIR\n"
   "    size_t n = strlen(p);\n"
   "    size_t k;\n"
   "    char *t = (char *)malloc(n + 1);\n"
   "    if (!t) abort();\n"
   "    memcpy(t, p, n + 1);\n"
   "    for (k = 1; k <= n; k = k + 1) {\n"
   "        if (k == n || t[k] == '/') {\n"
   "            char c = t[k];\n"
   "            t[k] = 0;\n"
   "            if (_engine_fs_kind(t) == 0) mkdir(t, 0755);\n"
   "            t[k] = c;\n"
   "        }\n"
   "    }\n"
   "    free(t);\n"
   "#else\n"
   "    (void)p;\n"
   "#endif\n"
   "    if (_engine_fs_kind(p) != 1)\n"
   "        _cs_throw(\"IOException: could not create the directory\");\n}",
   deps=("_engine_fs",))
_h("File_ReadAllText", "string",
   "static const char *File_ReadAllText(const char *p) {\n"
   "    FILE *f = fopen(p, \"rb\");\n"
   "    fastring t;\n"
   "    char buf[4096];\n"
   "    size_t n;\n"
   "    if (!f) _cs_throw(\"FileNotFoundException: Could not find file\");\n"
   "    n = fread(buf, 1, sizeof buf, f);\n"
   "    while (n > 0) {\n"
   "        t.append(buf, n);\n"
   "        n = fread(buf, 1, sizeof buf, f);\n"
   "    }\n"
   "    fclose(f);\n"
   "    /* A UTF-8 byte order mark is not text, as .NET reads it. */\n"
   "    if (t.size() >= 3 && (unsigned char)t[0] == 0xEF\n"
   "        && (unsigned char)t[1] == 0xBB && (unsigned char)t[2] == 0xBF)\n"
   "        return _engine_str_keep(t.data() + 3, t.size() - 3);\n"
   "    return _engine_str_keep(t.data(), t.size());\n}")
_h("File_ReadAllLines", "strarray",
   "static std::vector<fastring> File_ReadAllLines(const char *p) {\n"
   "    std::vector<fastring> v;\n"
   "    const char *s = File_ReadAllText(p);\n"
   "    const char *nl;\n"
   "    size_t n;\n"
   "    while (*s) {\n"
   "        nl = strchr(s, '\\n');\n"
   "        n = nl ? (size_t)(nl - s) : strlen(s);\n"
   "        if (n > 0 && s[n - 1] == '\\r') n = n - 1;\n"
   "        {\n"
   "            fastring e(s, n);\n"
   "            v.push_back(e);\n"
   "        }\n"
   "        if (!nl) break;\n"
   "        s = nl + 1;\n"
   "    }\n"
   "    return v;\n}", deps=("File_ReadAllText",))

# -- new string[n] -----------------------------------------------------------

_h("_cs_strarray_new", "strarray",
   "static std::vector<fastring> _cs_strarray_new(int n) {\n"
   "    std::vector<fastring> v;\n"
   "    fastring e;\n"
   "    int k;\n"
   "    if (n < 0) _cs_throw(\"OverflowException: array size is negative\");\n"
   "    for (k = 0; k < n; k = k + 1) v.push_back(e);\n"
   "    return v;\n}")

# -- string.GetHashCode -----------------------------------------------------

_h("_cs_str_GetHashCode", "int",
   "/* Deterministic (FNV-1a); .NET's is randomized per process, Mono's is\n"
   "   another function -- only equality of hashes is meaningful. */\n"
   "static int _cs_str_GetHashCode(const char *s) {\n"
   "    unsigned h = 2166136261u;\n"
   "    for (; *s; s = s + 1) h = (h ^ (unsigned char)*s) * 16777619u;\n"
   "    return (int)h;\n}")


def helper_c(name):
    return _M[name][0]


def helper_kind(name):
    return _M[name][1]


def helpers_of_kind(kind):
    """Helpers whose result is `kind` ("float", "int", "bool", "string",
    "strarray")."""
    return {n for n, v in _M.items() if v[1] == kind}


def needs_math(used):
    return any(_M[n][3] for n in used if n in _M)


def closure(used):
    """`used` with every helper they call, dependencies first."""
    order, seen = [], set()

    def visit(n):
        if n in seen or n not in _M:
            return
        seen.add(n)
        for d in _M[n][2]:
            visit(d)
        order.append(n)
    for n in sorted(used):
        visit(n)
    return order


def is_runtime_helper(name):
    return name in _M


# ---------------------------------------------------------------------------
# The C# spellings
# ---------------------------------------------------------------------------

#: `Type.Name` with n arguments -> helper. None for a property.
_CALLS = {
    ("Mathf", "Sqrt", 1): "Mathf_Sqrt", ("Mathf", "Pow", 2): "Mathf_Pow",
    ("Mathf", "Floor", 1): "Mathf_Floor", ("Mathf", "Ceil", 1): "Mathf_Ceil",
    ("Mathf", "Round", 1): "Mathf_Round",
    ("Mathf", "FloorToInt", 1): "Mathf_FloorToInt",
    ("Mathf", "CeilToInt", 1): "Mathf_CeilToInt",
    ("Mathf", "RoundToInt", 1): "Mathf_RoundToInt",
    ("Mathf", "Tan", 1): "Mathf_Tan", ("Mathf", "Asin", 1): "Mathf_Asin",
    ("Mathf", "Acos", 1): "Mathf_Acos", ("Mathf", "Atan", 1): "Mathf_Atan",
    ("Mathf", "Atan2", 2): "Mathf_Atan2", ("Mathf", "Exp", 1): "Mathf_Exp",
    ("Mathf", "Log", 1): "Mathf_Log", ("Mathf", "Log", 2): "Mathf_LogB",
    ("Mathf", "Log10", 1): "Mathf_Log10",
    ("Mathf", "Clamp01", 1): "Mathf_Clamp01",
    ("Mathf", "InverseLerp", 3): "Mathf_InverseLerp",
    ("Mathf", "LerpUnclamped", 3): "Mathf_LerpUnclamped",
    ("Mathf", "MoveTowards", 3): "Mathf_MoveTowards",
    ("Mathf", "Repeat", 2): "Mathf_Repeat",
    ("Mathf", "PingPong", 2): "Mathf_PingPong",
    ("Mathf", "DeltaAngle", 2): "Mathf_DeltaAngle",
    ("Mathf", "SmoothStep", 3): "Mathf_SmoothStep",
    ("Mathf", "Approximately", 2): "Mathf_Approximately",
    ("Random", "InitState", 1): "Random_InitState",
    ("Random", "value", None): "Random_value",
    ("int", "Parse", 1): "_cs_int_Parse", ("Int32", "Parse", 1): "_cs_int_Parse",
    ("float", "Parse", 1): "_cs_float_Parse",
    ("Single", "Parse", 1): "_cs_float_Parse",
    ("double", "Parse", 1): "_cs_float_Parse",
    ("Path", "Combine", 2): "Path_Combine",
    ("Path", "GetFileName", 1): "Path_GetFileName",
    ("Path", "GetExtension", 1): "Path_GetExtension",
    ("Path", "GetFileNameWithoutExtension", 1):
        "Path_GetFileNameWithoutExtension",
    ("Path", "GetDirectoryName", 1): "Path_GetDirectoryName",
    ("Directory", "Exists", 1): "Directory_Exists",
    ("Directory", "CreateDirectory", 1): "Directory_CreateDirectory",
    ("File", "ReadAllText", 1): "File_ReadAllText",
    ("File", "ReadAllLines", 1): "File_ReadAllLines",
}

_TYPES = sorted({k[0] for k in _CALLS} | {"Mathf", "Random", "Time"},
                key=len, reverse=True)
#: A qualifier C# lets a script write in front of these types.
_QUAL = r"(?:(?:UnityEngine|System(?:\s*\.\s*IO)?)\s*\.\s*)?"

#: Spellings that route the API scan here, for `analyze_script`.
API_RE = re.compile(
    r"(?<![\w.])" + _QUAL + r"(?:%s)\s*\.\s*(?:%s)\b" % (
        "|".join(re.escape(t) for t in _TYPES),
        "|".join(sorted({re.escape(k[1]) for k in _CALLS}
                        | set(MATHF_CONSTANTS) | {"Range", "TryParse",
                                                  "realtimeSinceStartup",
                                                  "unscaledTime"}))))


def lower_runtime_apis(text, used, blank, split_args, operand_kind,
                       match_close):
    """Lower the table's C# spellings in a method body (still C#).

    `used` collects the helpers; the callables are cs2cpp's: `blank` (a
    literal- and comment-blanked copy), `split_args`, `operand_kind(expr)`
    -> "i" / "f" / "s" / .. for `Random.Range`'s int-or-float choice, and
    `match_close(scan, k, open, close)`.

        Mathf.Sqrt(x)            ->  Mathf_Sqrt(x)
        Mathf.PI                 ->  3.14159274f
        Random.Range(0, 10)      ->  Random_Range_i(0, 10)   (both int)
        Random.Range(0f, 1f)     ->  Random_Range_f(0f, 1f)
        int.Parse(s)             ->  _cs_int_Parse(s)
        int.TryParse(s, out n)   ->  _cs_int_TryParse(s, &n)
        int.TryParse(s, out int n)   (declares `int n = 0;` before the
                                      statement, as C# scopes it there)
        Time.realtimeSinceStartup ->  Time.time
    """
    # Time: the packed engine has no time scale and reads no wall clock.
    text = _sub(text, blank, r"(?<![\w.])" + _QUAL +
                r"Time\s*\.\s*(?:realtimeSinceStartup|unscaledTime)"
                r"(?:AsDouble)?\b", lambda m: "Time.time")
    # Mathf constants
    for name, val in MATHF_CONSTANTS.items():
        def rep(m, v=val):
            if "Mathf_Infinity" in v:
                used.add("Mathf_Infinity")
            return v
        text = _sub(text, blank, r"(?<![\w.])" + _QUAL +
                    r"Mathf\s*\.\s*%s\b(?!\s*\()" % name, rep)
    # Calls and properties
    call_re = re.compile(r"(?<![\w.])" + _QUAL + r"(%s)\s*\.\s*(\w+)\b" %
                         "|".join(re.escape(t) for t in _TYPES))
    start = 0
    for _pass in range(512):
        scan = blank(text)
        m = call_re.search(scan, start)
        if not m:
            break
        ty, name = m.group(1), m.group(2)
        k = m.end()
        while k < len(scan) and scan[k] in " \t":
            k += 1
        if k < len(scan) and scan[k] == "(":
            cl = match_close(scan, k, "(", ")")
            if cl is None:
                start = m.end()
                continue
            inner = text[k + 1:cl]
            args = [a.strip() for a in split_args(inner)] if inner.strip() \
                else []
            rep = _lower_call(ty, name, args, used, operand_kind)
            if rep is None:
                start = m.end()
                continue
            if isinstance(rep, tuple):
                rep, decl = rep
                s0 = _statement_start(scan, m.start())
                text = (text[:s0] + decl + text[s0:m.start()] + rep
                        + text[cl + 1:])
                start = s0
                continue
            text = text[:m.start()] + rep + text[cl + 1:]
            start = m.start()
            continue
        helper = _CALLS.get((ty, name, None))
        if helper:
            used.add(helper)
            text = text[:m.start()] + helper + "()" + text[m.end():]
        start = m.start() + 1
    return text


def _lower_call(ty, name, args, used, operand_kind):
    if ty == "Random" and name == "Range" and len(args) == 2:
        kinds = [operand_kind(a) for a in args]
        helper = "Random_Range_i" if all(k == "i" for k in kinds) \
            else "Random_Range_f"
        used.add(helper)
        return "%s(%s)" % (helper, ", ".join(args))
    if name == "TryParse" and ty in ("int", "Int32", "float", "Single",
                                     "double") and len(args) == 2:
        m = re.match(r"^out\s+(?:(int|float|double|var)\s+)?(\w+)$", args[1])
        if not m:
            return None
        helper = "_cs_int_TryParse" if ty in ("int", "Int32") \
            else "_cs_float_TryParse"
        used.add(helper)
        call = "%s(%s, &%s)" % (helper, args[0], m.group(2))
        if m.group(1):
            cty = "int" if ty in ("int", "Int32") else "float"
            return call, "%s %s = 0; " % (cty, m.group(2))
        return call
    if ty == "Path" and name == "Combine" and len(args) > 2:
        used.add("Path_Combine")
        out = args[0]
        for a in args[1:]:
            out = "Path_Combine(%s, %s)" % (out, a)
        return out
    helper = _CALLS.get((ty, name, len(args)))
    if helper is None:
        return None
    used.add(helper)
    return "%s(%s)" % (helper, ", ".join(args))


def _statement_start(scan, k):
    """Start of the statement holding index k (after the `;`, `{` or `}`
    before it, at its depth)."""
    depth = 0
    j = k - 1
    while j >= 0:
        c = scan[j]
        if c in ")]":
            depth += 1
        elif c in "([":
            if depth == 0:
                pass
            else:
                depth -= 1
        elif c in ";{}" and depth == 0:
            break
        j -= 1
    j += 1
    while j < k and scan[j] in " \t\r\n":
        j += 1
    return j


def _sub(text, blank, pat, fn):
    """Substitute outside literals and comments."""
    scan = blank(text)
    out, last = [], 0
    for m in re.finditer(pat, scan):
        out.append(text[last:m.start()])
        out.append(fn(m))
        last = m.end()
    out.append(text[last:])
    return "".join(out)
