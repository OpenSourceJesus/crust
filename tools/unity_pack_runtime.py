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
_h("Random_insideUnitCircle", "Vector2",
   "static Vector2 Random_insideUnitCircle(void) {\n"
   "    float x, y;\n"
   "    while (1) {\n"
   "        x = 2.f * Random_value() - 1.f;\n"
   "        y = 2.f * Random_value() - 1.f;\n"
   "        if (x * x + y * y <= 1.f) return Vector2_make(x, y);\n"
   "    }\n}", deps=("Random_value",))
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
_M["_cs_read_decimal"] = (_M["_cs_read_decimal"][0] + (
    "\nstatic double _cs_read_decimal_s(const char *s) {\n"
    "    char *end;\n    return _cs_read_decimal(s, &end);\n}"),
    "void", (), False)
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

# -- JsonUtility: the pieces unity_pack's per-class ToJson / FromJson use --

_h("_cs_json_w", "void",
   "/* JsonUtility writing: Unity's compact form, or its pretty one (4-space\n"
   "   indent, `\"key\": value`). A float always has a point, as Unity's does. */\n"
   "static void _cs_json_key(fastring *t, const char *k, int first, int pretty,\n"
   "                         int depth) {\n"
   "    int d;\n"
   "    if (!first) t->append_char(',');\n"
   "    if (pretty) {\n"
   "        t->append_char('\\n');\n"
   "        for (d = 0; d < depth; d = d + 1) t->append_cstr(\"    \");\n"
   "    }\n"
   "    t->append_char('\"');\n"
   "    t->append_cstr(k);\n"
   "    t->append_cstr(pretty ? \"\\\": \" : \"\\\":\");\n"
   "}\n"
   "static void _cs_json_close(fastring *t, int pretty, int depth) {\n"
   "    int d;\n"
   "    if (pretty) {\n"
   "        t->append_char('\\n');\n"
   "        for (d = 0; d < depth; d = d + 1) t->append_cstr(\"    \");\n"
   "    }\n"
   "    t->append_char('}');\n"
   "}\n"
   "static void _cs_json_int(fastring *t, int v) {\n"
   "    char b[16];\n"
   "    snprintf(b, sizeof b, \"%d\", v);\n"
   "    t->append_cstr(b);\n"
   "}\n"
   "static void _cs_json_bool(fastring *t, int v) {\n"
   "    t->append_cstr(v ? \"true\" : \"false\");\n"
   "}\n"
   "/* The shortest decimal that reads back as the same float. */\n"
   "static void _cs_json_float(fastring *t, float v) {\n"
   "    char b[40];\n"
   "    int p;\n"
   "    for (p = 6; p <= 9; p = p + 1) {\n"
   "        snprintf(b, sizeof b, \"%.*g\", p, (double)v);\n"
   "        if ((float)_cs_read_decimal_s(b) == v) break;\n"
   "    }\n"
   "    t->append_cstr(b);\n"
   "    if (!strchr(b, '.') && !strchr(b, 'e') && !strchr(b, 'n')\n"
   "        && !strchr(b, 'i')) t->append_cstr(\".0\");\n"
   "}\n"
   "static void _cs_json_str(fastring *t, const char *s) {\n"
   "    char b[8];\n"
   "    t->append_char('\"');\n"
   "    for (; *s; s = s + 1) {\n"
   "        unsigned char c = (unsigned char)*s;\n"
   "        if (c == '\"') t->append_cstr(\"\\\\\\\"\");\n"
   "        else if (c == '\\\\') t->append_cstr(\"\\\\\\\\\");\n"
   "        else if (c == '\\n') t->append_cstr(\"\\\\n\");\n"
   "        else if (c == '\\r') t->append_cstr(\"\\\\r\");\n"
   "        else if (c == '\\t') t->append_cstr(\"\\\\t\");\n"
   "        else if (c < 0x20) {\n"
   "            snprintf(b, sizeof b, \"\\\\u%04x\", (unsigned)c);\n"
   "            t->append_cstr(b);\n"
   "        } else t->append_char((char)c);\n"
   "    }\n"
   "    t->append_char('\"');\n"
   "}", deps=("_cs_read_decimal",))
_h("_cs_json_r", "void",
   "/* JsonUtility reading: the value of `key` in the object at `o`, or 0.\n"
   "   Nested objects and arrays are skipped over; a document that is not\n"
   "   an object is .NET's ArgumentException. */\n"
   "static const char *_cs_json_ws(const char *p) {\n"
   "    while (*p == ' ' || *p == '\\t' || *p == '\\n' || *p == '\\r') p = p + 1;\n"
   "    return p;\n"
   "}\n"
   "static const char *_cs_json_skip(const char *p) {\n"
   "    int depth = 0;\n"
   "    p = _cs_json_ws(p);\n"
   "    do {\n"
   "        if (*p == '\"') {\n"
   "            p = p + 1;\n"
   "            while (*p && *p != '\"') { if (*p == '\\\\' && p[1]) p = p + 1; p = p + 1; }\n"
   "            if (*p) p = p + 1;\n"
   "        } else if (*p == '{' || *p == '[') { depth = depth + 1; p = p + 1; }\n"
   "        else if (*p == '}' || *p == ']') { depth = depth - 1; p = p + 1; }\n"
   "        else if (*p == 0) return p;\n"
   "        else {\n"
   "            while (*p && *p != ',' && *p != '}' && *p != ']' && *p != '\"'\n"
   "                   && *p != '{' && *p != '[') p = p + 1;\n"
   "        }\n"
   "        if (depth > 0) {\n"
   "            p = _cs_json_ws(p);\n"
   "            if (*p == ',' || *p == ':') p = p + 1;\n"
   "        }\n"
   "    } while (depth > 0 && *p);\n"
   "    return p;\n"
   "}\n"
   "static const char *_cs_json_find(const char *o, const char *key) {\n"
   "    const char *p = _cs_json_ws(o);\n"
   "    size_t n = strlen(key);\n"
   "    if (*p != '{') _cs_throw(\"ArgumentException: JSON parse error: not an object\");\n"
   "    p = p + 1;\n"
   "    for (;;) {\n"
   "        const char *k;\n"
   "        p = _cs_json_ws(p);\n"
   "        if (*p != '\"') return 0;\n"
   "        k = p + 1;\n"
   "        p = k;\n"
   "        while (*p && *p != '\"') { if (*p == '\\\\' && p[1]) p = p + 1; p = p + 1; }\n"
   "        if (!*p) return 0;\n"
   "        {\n"
   "            int hit = (size_t)(p - k) == n && strncmp(k, key, n) == 0;\n"
   "            p = _cs_json_ws(p + 1);\n"
   "            if (*p != ':') return 0;\n"
   "            p = _cs_json_ws(p + 1);\n"
   "            if (hit) return p;\n"
   "        }\n"
   "        p = _cs_json_ws(_cs_json_skip(p));\n"
   "        if (*p != ',') return 0;\n"
   "        p = p + 1;\n"
   "    }\n"
   "}\n"
   "static int _cs_json_read_int(const char *p) {\n"
   "    char *end;\n"
   "    return (int)strtol(p, &end, 10);\n"
   "}\n"
   "static int _cs_json_read_bool(const char *p) {\n"
   "    return strncmp(p, \"true\", 4) == 0 || (*p >= '1' && *p <= '9');\n"
   "}\n"
   "static float _cs_json_read_float(const char *p) {\n"
   "    return (float)_cs_read_decimal_s(p);\n"
   "}\n"
   "static const char *_cs_json_read_str(const char *p) {\n"
   "    fastring t;\n"
   "    if (*p != '\"') return \"\";\n"
   "    p = p + 1;\n"
   "    while (*p && *p != '\"') {\n"
   "        if (*p == '\\\\' && p[1]) {\n"
   "            p = p + 1;\n"
   "            if (*p == 'n') t.append_char('\\n');\n"
   "            else if (*p == 'r') t.append_char('\\r');\n"
   "            else if (*p == 't') t.append_char('\\t');\n"
   "            else if (*p == 'b') t.append_char('\\b');\n"
   "            else if (*p == 'f') t.append_char('\\f');\n"
   "            else if (*p == 'u') {\n"
   "                unsigned u = 0;\n"
   "                int k;\n"
   "                for (k = 1; k <= 4 && p[k]; k = k + 1) {\n"
   "                    char c = p[k];\n"
   "                    u = u * 16u + (unsigned)(c >= 'a' ? c - 'a' + 10\n"
   "                        : c >= 'A' ? c - 'A' + 10 : c - '0');\n"
   "                }\n"
   "                p = p + 4;\n"
   "                if (u < 0x80u) t.append_char((char)u);\n"
   "                else if (u < 0x800u) {\n"
   "                    t.append_char((char)(0xC0u | (u >> 6)));\n"
   "                    t.append_char((char)(0x80u | (u & 0x3Fu)));\n"
   "                } else {\n"
   "                    t.append_char((char)(0xE0u | (u >> 12)));\n"
   "                    t.append_char((char)(0x80u | ((u >> 6) & 0x3Fu)));\n"
   "                    t.append_char((char)(0x80u | (u & 0x3Fu)));\n"
   "                }\n"
   "            } else t.append_char(*p);\n"
   "        } else t.append_char(*p);\n"
   "        p = p + 1;\n"
   "    }\n"
   "    return _engine_str_keep(t.data(), t.size());\n"
   "}", deps=("_cs_read_decimal",))

# -- Clock: Stopwatch, DateTime.Now ------------------------------------------
# The player is built with gcc: <time.h> is there. Behind the POSIX guard
# like the file helpers, so the pack's validation through crust's own C
# front end (which has no <time.h>) still passes; there the clock reads 0.

_h("_engine_clock", "void",
   "#ifndef CRUST_NO_POSIX_MKDIR\n#include <time.h>\n#endif\n"
   "/* Monotonic seconds, for Stopwatch. */\n"
   "static double _engine_clock_s(void) {\n"
   "#ifndef CRUST_NO_POSIX_MKDIR\n"
   "    struct timespec ts;\n"
   "    clock_gettime(CLOCK_MONOTONIC, &ts);\n"
   "    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;\n"
   "#else\n"
   "    return 0.0;\n"
   "#endif\n}\n"
   "/* Wall-clock seconds since the epoch, for DateTime.Now. */\n"
   "static double _engine_wall_s(void) {\n"
   "#ifndef CRUST_NO_POSIX_MKDIR\n"
   "    struct timespec ts;\n"
   "    clock_gettime(CLOCK_REALTIME, &ts);\n"
   "    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;\n"
   "#else\n"
   "    return 0.0;\n"
   "#endif\n}")
_h("_cs_stopwatch", "void",
   "typedef struct { double start; double acc; int running; } _cs_stopwatch;\n"
   "static _cs_stopwatch _cs_sw_new(int run) {\n"
   "    _cs_stopwatch w;\n"
   "    w.acc = 0.0; w.running = run; w.start = run ? _engine_clock_s() : 0.0;\n"
   "    return w;\n}\n"
   "static double _cs_sw_elapsed(_cs_stopwatch *w) {\n"
   "    return w->acc + (w->running ? _engine_clock_s() - w->start : 0.0);\n}\n"
   "static void _cs_sw_start(_cs_stopwatch *w) {\n"
   "    if (!w->running) { w->start = _engine_clock_s(); w->running = 1; }\n}\n"
   "static void _cs_sw_stop(_cs_stopwatch *w) {\n"
   "    if (w->running) { w->acc = _cs_sw_elapsed(w); w->running = 0; }\n}\n"
   "static void _cs_sw_reset(_cs_stopwatch *w) { w->acc = 0.0; w->running = 0; }\n"
   "static void _cs_sw_restart(_cs_stopwatch *w) {\n"
   "    w->acc = 0.0; w->start = _engine_clock_s(); w->running = 1;\n}\n"
   "static int _cs_sw_ms(_cs_stopwatch *w) { return (int)(_cs_sw_elapsed(w) * 1000.0); }\n"
   "static float _cs_sw_s(_cs_stopwatch *w) { return (float)_cs_sw_elapsed(w); }\n"
   "static float _cs_sw_total_ms(_cs_stopwatch *w) {\n"
   "    return (float)(_cs_sw_elapsed(w) * 1000.0);\n}\n"
   "static int _cs_sw_running(_cs_stopwatch *w) { return w->running; }",
   deps=("_engine_clock",))
_h("_cs_datetime", "void",
   "/* DateTime: seconds since the epoch, and whether it is UTC. */\n"
   "typedef struct { double t; int utc; } _cs_datetime;\n"
   "static _cs_datetime _cs_dt_now(void) {\n"
   "    _cs_datetime d; d.t = _engine_wall_s(); d.utc = 0; return d;\n}\n"
   "static _cs_datetime _cs_dt_utcnow(void) {\n"
   "    _cs_datetime d; d.t = _engine_wall_s(); d.utc = 1; return d;\n}\n"
   "/* 0 year, 1 month, 2 day, 3 hour, 4 minute, 5 second, 6 millisecond,\n"
   "   7 day of year, 8 day of week (0 Sunday) */\n"
   "static int _cs_dt_part(_cs_datetime d, int which) {\n"
   "#ifndef CRUST_NO_POSIX_MKDIR\n"
   "    time_t s = (time_t)d.t;\n"
   "    struct tm tmv;\n"
   "    if (d.utc) gmtime_r(&s, &tmv); else localtime_r(&s, &tmv);\n"
   "    if (which == 0) return tmv.tm_year + 1900;\n"
   "    if (which == 1) return tmv.tm_mon + 1;\n"
   "    if (which == 2) return tmv.tm_mday;\n"
   "    if (which == 3) return tmv.tm_hour;\n"
   "    if (which == 4) return tmv.tm_min;\n"
   "    if (which == 5) return tmv.tm_sec;\n"
   "    if (which == 6) return (int)((d.t - (double)s) * 1000.0);\n"
   "    if (which == 7) return tmv.tm_yday + 1;\n"
   "    return tmv.tm_wday;\n"
   "#else\n"
   "    (void)d; (void)which;\n"
   "    return 0;\n"
   "#endif\n}\n"
   "/* .NET custom date format: yyyy yy MM M dd d HH H hh h mm m ss s fff ff f\n"
   "   tt, 'quoted' text, \\\\x; anything else as written. */\n"
   "static const char *_cs_dt_format(_cs_datetime d, const char *f) {\n"
   "    fastring t;\n"
   "    char b[16];\n"
   "    int n;\n"
   "    while (*f) {\n"
   "        char c = *f;\n"
   "        n = 1;\n"
   "        while (f[n] == c) n = n + 1;\n"
   "        if (c == 'y') {\n"
   "            if (n <= 2) snprintf(b, sizeof b, \"%02d\", _cs_dt_part(d, 0) % 100);\n"
   "            else snprintf(b, sizeof b, \"%04d\", _cs_dt_part(d, 0));\n"
   "            t.append_cstr(b);\n"
   "        } else if (c == 'M' || c == 'd' || c == 'H' || c == 'm' || c == 's'\n"
   "                   || c == 'h') {\n"
   "            int v = c == 'M' ? _cs_dt_part(d, 1) : c == 'd' ? _cs_dt_part(d, 2)\n"
   "                : c == 'm' ? _cs_dt_part(d, 4) : c == 's' ? _cs_dt_part(d, 5)\n"
   "                : _cs_dt_part(d, 3);\n"
   "            if (c == 'h') { v = v % 12; if (v == 0) v = 12; }\n"
   "            snprintf(b, sizeof b, n >= 2 ? \"%02d\" : \"%d\", v);\n"
   "            t.append_cstr(b);\n"
   "        } else if (c == 'f') {\n"
   "            int ms = _cs_dt_part(d, 6);\n"
   "            if (n >= 3) snprintf(b, sizeof b, \"%03d\", ms);\n"
   "            else if (n == 2) snprintf(b, sizeof b, \"%02d\", ms / 10);\n"
   "            else snprintf(b, sizeof b, \"%d\", ms / 100);\n"
   "            t.append_cstr(b);\n"
   "        } else if (c == 't') {\n"
   "            t.append_cstr(_cs_dt_part(d, 3) < 12 ? (n >= 2 ? \"AM\" : \"A\")\n"
   "                                                : (n >= 2 ? \"PM\" : \"P\"));\n"
   "        } else if (c == '\\'' || c == '\"') {\n"
   "            n = 1;\n"
   "            while (f[n] && f[n] != c) { t.append_char(f[n]); n = n + 1; }\n"
   "            if (f[n]) n = n + 1;\n"
   "        } else if (c == '\\\\' && f[1]) {\n"
   "            t.append_char(f[1]);\n"
   "            n = 2;\n"
   "        } else {\n"
   "            int k;\n"
   "            for (k = 0; k < n; k = k + 1) t.append_char(c);\n"
   "        }\n"
   "        f = f + n;\n"
   "    }\n"
   "    return _engine_str_keep(t.data(), t.size());\n}",
   deps=("_engine_clock",))

# The helpers a script's calls become, with their result kinds.
for _n, _k in (("_cs_sw_ms", "int"), ("_cs_sw_s", "float"),
               ("_cs_sw_total_ms", "float"), ("_cs_sw_running", "bool"),
               ("_cs_dt_part", "int"), ("_cs_dt_format", "string")):
    _M[_n] = ("", _k, (("_cs_stopwatch",) if _n.startswith("_cs_sw")
                        else ("_cs_datetime",)), False)

# -- Stack.Pop / Queue.Dequeue on an empty one ------------------------------

_h("_cs_require_nonempty", "void",
   "static void _cs_require_nonempty(int n, const char *what) {\n"
   "    if (n <= 0) {\n"
   "        fprintf(stderr, \"Unhandled exception: InvalidOperationException: \"\n"
   "                \"%s empty.\\n\", what);\n"
   "        fflush(stderr);\n"
   "        abort();\n"
   "    }\n}")

# -- transform.eulerAngles.z of a live rotation ----------------------------------

_h("_cs_euler_z", "float",
   "/* A rotation about z (quaternion z, w) in degrees, [0, 360) as Unity's\n"
   "   eulerAngles reports it. */\n"
   "static float _cs_euler_z(float qz, float qw) {\n"
   "    float d = (float)(2.0 * atan2((double)qz, (double)qw) * 57.29577951308232);\n"
   "    while (d < 0.f) d = d + 360.f;\n"
   "    while (d >= 360.f) d = d - 360.f;\n"
   "    return d;\n}", math=True)

# -- LinkedList ends -----------------------------------------------------------

_h("_cs_ll_first", "int",
   "static int _cs_ll_first(int n) {\n"
   "    if (n <= 0) {\n"
   "        fprintf(stderr, \"Unhandled exception: \"\n"
   "                \"InvalidOperationException: The LinkedList is empty.\\n\");\n"
   "        fflush(stderr);\n"
   "        abort();\n"
   "    }\n"
   "    return 0;\n}")
_h("_cs_ll_last", "int",
   "static int _cs_ll_last(int n) {\n"
   "    return _cs_ll_first(n) + n - 1;\n}", deps=("_cs_ll_first",))

# -- byte[] as a vector of ints (a List<byte>) --------------------------------
# Hashes and Base64 are coost's (md5digest_to, sha256digest_to,
# base64_encode / _decode): the pack splices their sources in when used.

_h("_cs_bytes_to_c", "void",
   "/* A byte list's bytes, contiguous, for coost (a scratch buffer). */\n"
   "static const char *_cs_bytes_raw(std::vector<int> &v) {\n"
   "    char *out = _engine_str_slot(v.size() + 1);\n"
   "    int k;\n"
   "    for (k = 0; k < (int)v.size(); k = k + 1) out[k] = (char)v[k];\n"
   "    out[v.size()] = 0;\n"
   "    return out;\n}\n"
   "static std::vector<int> _cs_bytes_of(const char *p, size_t n) {\n"
   "    std::vector<int> v;\n"
   "    size_t k;\n"
   "    for (k = 0; k < n; k = k + 1) {\n"
   "        int b = (int)(unsigned char)p[k];\n"
   "        v.push_back(b);\n"
   "    }\n"
   "    return v;\n}")
_h("_cs_bytes_utf8", "bytes",
   "static std::vector<int> _cs_bytes_utf8(const char *s) {\n"
   "    return _cs_bytes_of(s, strlen(s));\n}", deps=("_cs_bytes_to_c",))
_h("_cs_bytes_to_string", "string",
   "static const char *_cs_bytes_to_string(std::vector<int> &v) {\n"
   "    return _cs_bytes_raw(v);\n}", deps=("_cs_bytes_to_c",))
_h("_cs_bytes_base64", "string",
   "static const char *_cs_bytes_base64(std::vector<int> &v) {\n"
   "    fastring t = base64_encode(_cs_bytes_raw(v), v.size());\n"
   "    return _engine_str_keep(t.data(), t.size());\n}",
   deps=("_cs_bytes_to_c",))
_h("_cs_bytes_unbase64", "bytes",
   "static std::vector<int> _cs_bytes_unbase64(const char *s) {\n"
   "    fastring t = base64_decode(s, strlen(s));\n"
   "    if (t.size() == 0 && s[0])\n"
   "        _cs_throw(\"FormatException: The input is not a valid Base-64 string\");\n"
   "    return _cs_bytes_of(t.data(), t.size());\n}",
   deps=("_cs_bytes_to_c",))
_h("_cs_bytes_md5", "bytes",
   "static std::vector<int> _cs_bytes_md5(std::vector<int> &v) {\n"
   "    char d[16];\n"
   "    md5digest_to(_cs_bytes_raw(v), v.size(), d);\n"
   "    return _cs_bytes_of(d, 16);\n}", deps=("_cs_bytes_to_c",))
_h("_cs_bytes_sha256", "bytes",
   "static std::vector<int> _cs_bytes_sha256(std::vector<int> &v) {\n"
   "    char d[32];\n"
   "    sha256digest_to(_cs_bytes_raw(v), v.size(), d);\n"
   "    return _cs_bytes_of(d, 32);\n}", deps=("_cs_bytes_to_c",))
_h("_cs_bytes_hex_dash", "string",
   "static const char *_cs_bytes_hex_dash(std::vector<int> &v) {\n"
   "    fastring t;\n"
   "    char b[4];\n"
   "    int k;\n"
   "    for (k = 0; k < (int)v.size(); k = k + 1) {\n"
   "        if (k > 0) t.append_char('-');\n"
   "        snprintf(b, sizeof b, \"%02X\", (unsigned)(v[k] & 255));\n"
   "        t.append_cstr(b);\n"
   "    }\n"
   "    return _engine_str_keep(t.data(), t.size());\n}")
_h("_cs_file_read_bytes", "bytes",
   "static std::vector<int> _cs_file_read_bytes(const char *p) {\n"
   "    std::vector<int> v;\n"
   "    FILE *f = fopen(p, \"rb\");\n"
   "    int c;\n"
   "    if (!f) _cs_throw(\"FileNotFoundException: Could not find file\");\n"
   "    c = fgetc(f);\n"
   "    while (c != -1) {\n"
   "        v.push_back(c);\n"
   "        c = fgetc(f);\n"
   "    }\n"
   "    fclose(f);\n"
   "    return v;\n}")
_h("_cs_file_write_bytes", "void",
   "static void _cs_file_write_bytes(const char *p, std::vector<int> &v) {\n"
   "    FILE *f = fopen(p, \"wb\");\n"
   "    int k;\n"
   "    if (!f) _cs_throw(\"IOException: could not write the file\");\n"
   "    for (k = 0; k < (int)v.size(); k = k + 1) fputc(v[k] & 255, f);\n"
   "    fclose(f);\n}")

# -- T[,] / T[,,] element index ------------------------------------------------

_h("_cs_idx_fail", "void",
   "static int _cs_idx_fail(void) {\n"
   "    fprintf(stderr, \"Unhandled exception: IndexOutOfRangeException: \"\n"
   "            \"Index was outside the bounds of the array.\\n\");\n"
   "    fflush(stderr);\n"
   "    abort();\n"
   "    return 0;\n}")
_h("_cs_idx2", "int",
   "static int _cs_idx2(int x, int d0, int y, int d1) {\n"
   "    if (x < 0 || x >= d0 || y < 0 || y >= d1) return _cs_idx_fail();\n"
   "    return x * d1 + y;\n}", deps=("_cs_idx_fail",))
_h("_cs_idx3", "int",
   "static int _cs_idx3(int x, int d0, int y, int d1, int z, int d2) {\n"
   "    if (x < 0 || x >= d0 || y < 0 || y >= d1 || z < 0 || z >= d2)\n"
   "        return _cs_idx_fail();\n"
   "    return (x * d1 + y) * d2 + z;\n}", deps=("_cs_idx_fail",))

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


_SW_T = r"(?:System\s*\.\s*Diagnostics\s*\.\s*)?Stopwatch"
_DT_T = r"(?:System\s*\.\s*)?DateTime"
_DT_PARTS = {"Year": 0, "Month": 1, "Day": 2, "Hour": 3, "Minute": 4,
             "Second": 5, "Millisecond": 6, "DayOfYear": 7}


def lower_clock(text, used, blank, match_close):
    """`Stopwatch` and `DateTime.Now` / `UtcNow`, in a method body (C#).

        var sw = Stopwatch.StartNew();   ->  _cs_stopwatch sw = _cs_sw_new(1);
        sw.Stop();  sw.ElapsedMilliseconds  ->  _cs_sw_stop(&sw);  _cs_sw_ms(&sw)
        sw.Elapsed.TotalSeconds          ->  _cs_sw_s(&sw)
        DateTime t = DateTime.Now;       ->  _cs_datetime t = _cs_dt_now();
        t.Year  DateTime.Now.Hour        ->  _cs_dt_part(t, 0)  _cs_dt_part(_cs_dt_now(), 3)
        t.ToString("yyyy-MM-dd")         ->  _cs_dt_format(t, "yyyy-MM-dd")
        t.ToString()                     ->  the invariant culture's general
                                             form, "MM/dd/yyyy HH:mm:ss"

    A stopwatch or date kept in a field, passed to a method, or subtracted
    (a TimeSpan) is not lowered, and the method is reported.
    """
    class _Groups(object):
        """A match on the blanked copy, its groups read from the text."""

        def __init__(self, m, t):
            self.m, self.t = m, t

        def group(self, k=0):
            a, b = self.m.span(k)
            return None if a < 0 else self.t[a:b]

    def sub(pat, fn):
        nonlocal text
        scan = blank(text)
        out, last = [], 0
        for m in re.finditer(pat, scan):
            out.append(text[last:m.start()])
            out.append(fn(_Groups(m, text)))
            last = m.end()
        out.append(text[last:])
        text = "".join(out)

    sws, dts = set(), set()
    # declarations
    def sw_decl(m):
        sws.add(m.group(1))
        used.add("_cs_stopwatch")
        return "_cs_stopwatch %s = _cs_sw_new(%d)" % (
            m.group(1), 1 if m.group(2) else 0)
    sub(r"(?<![\w.])(?:var|%s)\s+(\w+)\s*=\s*(?:%s\s*\.\s*(StartNew)\s*\(\s*\)"
        r"|new\s+%s\s*\(\s*\))" % (_SW_T, _SW_T, _SW_T), sw_decl)

    def dt_decl(m):
        dts.add(m.group(1))
        return "_cs_datetime %s = %s" % (m.group(1), m.group(2))
    sub(r"(?<![\w.])(?:var|%s)\s+(\w+)\s*=\s*(%s\s*\.\s*(?:Now|UtcNow)\b)"
        % (_DT_T, _DT_T), dt_decl)
    # DateTime.Now / UtcNow
    def dt_now(m):
        used.add("_cs_datetime")
        return "_cs_dt_%s()" % ("utcnow" if m.group(1) == "UtcNow" else "now")
    sub(r"(?<![\w.])%s\s*\.\s*(Now|UtcNow)\b" % _DT_T, dt_now)
    # stopwatch members
    if sws:
        alt = "|".join(re.escape(n) for n in sorted(sws))
        calls = {"Start": "_cs_sw_start", "Stop": "_cs_sw_stop",
                 "Reset": "_cs_sw_reset", "Restart": "_cs_sw_restart"}
        sub(r"(?<![\w.])(%s)\s*\.\s*(Start|Stop|Reset|Restart)\s*\(\s*\)" % alt,
            lambda m: "%s(&%s)" % (calls[m.group(2)], m.group(1)))
        props = {"ElapsedMilliseconds": "_cs_sw_ms", "IsRunning": "_cs_sw_running",
                 "Elapsed.TotalSeconds": "_cs_sw_s",
                 "Elapsed.TotalMilliseconds": "_cs_sw_total_ms"}

        def sw_prop(m):
            h = props[re.sub(r"\s", "", m.group(2))]
            used.add(h)
            return "%s(&%s)" % (h, m.group(1))
        sub(r"(?<![\w.])(%s)\s*\.\s*(ElapsedMilliseconds|IsRunning|"
            r"Elapsed\s*\.\s*TotalSeconds|Elapsed\s*\.\s*TotalMilliseconds)\b"
            % alt, sw_prop)
    # date members, on a date local or a Now / UtcNow
    recv = r"(_cs_dt_(?:now|utcnow)\s*\(\s*\)%s)" % (
        ("|" + "|".join(r"(?<![\w.])" + re.escape(n) for n in sorted(dts)))
        if dts else "")

    def dt_part(m):
        used.add("_cs_dt_part")
        return "_cs_dt_part(%s, %d)" % (m.group(1), _DT_PARTS[m.group(2)])
    sub(recv + r"\s*\.\s*(%s)\b" % "|".join(_DT_PARTS), dt_part)

    def dt_fmt(m):
        used.add("_cs_dt_format")
        arg = m.group(2).strip()
        return "_cs_dt_format(%s, %s)" % (
            m.group(1), arg if arg else '"MM/dd/yyyy HH:mm:ss"')
    sub(recv + r'\s*\.\s*ToString\s*\(\s*("(?:[^"\\]|\\.)*"|)\s*\)', dt_fmt)
    return text


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
        if _M[n][0]:                     # a name-only entry has no C
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
    ("Random", "insideUnitCircle", None): "Random_insideUnitCircle",
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
                                                  "unscaledTime",
                                                  "timeSinceLevelLoad",
                                                  "unscaledDeltaTime"}))))


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
    # ponytail: timeSinceLevelLoad is not reset by a scene load
    text = _sub(text, blank, r"(?<![\w.])" + _QUAL +
                r"Time\s*\.\s*(?:realtimeSinceStartup|unscaledTime|"
                r"timeSinceLevelLoad)(?:AsDouble)?\b", lambda m: "Time.time")
    text = _sub(text, blank, r"(?<![\w.])" + _QUAL +
                r"Time\s*\.\s*unscaledDeltaTime\b", lambda m: "Time.deltaTime")
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
