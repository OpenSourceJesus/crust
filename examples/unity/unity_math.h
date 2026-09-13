/* unity_math.h -- Mathf, Vector2, Vector3, Quaternion shaped like Unity's API.
 *
 * Value types only (no GC). Names follow UnityEngine; a few spellings bend
 * to what the Crust C++ subset lowers cleanly:
 *   - scalar multiply is `.scaled(s)` (eigen style) rather than `v * s`
 *   - quat∘quat is `.Multiply(q)`; quat rotates a vector with `.Rotate(v)`
 *   - static helpers take `const T &` like examples/eigen/la.h
 *
 * Build: see README.md
 */

#ifndef CRUST_UNITY_MATH_H
#define CRUST_UNITY_MATH_H

#include <math.h>

/* -------------------------------------------------------------------------- */
/* Mathf — static class; each method is self-contained (no static→static).   */
/* -------------------------------------------------------------------------- */

class Mathf {
public:
    static float PI() { return 3.14159265358979323846f; }
    static float Deg2Rad() { return 3.14159265358979323846f / 180.0f; }
    static float Rad2Deg() { return 180.0f / 3.14159265358979323846f; }

    static float Abs(float f) { return fabsf(f); }
    static float Sign(float f) {
        if (f > 0.0f) return 1.0f;
        if (f < 0.0f) return -1.0f;
        return 0.0f;
    }
    static float Min(float a, float b) { return a < b ? a : b; }
    static float Max(float a, float b) { return a > b ? a : b; }
    static float Clamp(float value, float min, float max) {
        if (value < min) return min;
        if (value > max) return max;
        return value;
    }
    static float Clamp01(float value) {
        if (value < 0.0f) return 0.0f;
        if (value > 1.0f) return 1.0f;
        return value;
    }
    static float Lerp(float a, float b, float t) {
        if (t < 0.0f) t = 0.0f;
        if (t > 1.0f) t = 1.0f;
        return a + (b - a) * t;
    }
    static float LerpUnclamped(float a, float b, float t) {
        return a + (b - a) * t;
    }
    static float InverseLerp(float a, float b, float value) {
        if (a == b) return 0.0f;
        float t = (value - a) / (b - a);
        if (t < 0.0f) return 0.0f;
        if (t > 1.0f) return 1.0f;
        return t;
    }
    static bool Approximately(float a, float b) {
        float d = fabsf(a - b);
        float aa = fabsf(a);
        float ab = fabsf(b);
        float m = aa > ab ? aa : ab;
        if (m < 1.0f) m = 1.0f;
        return d < 0.000001f * m;
    }
    static float Sqrt(float f) { return sqrtf(f); }
    static float Sin(float f) { return sinf(f); }
    static float Cos(float f) { return cosf(f); }
    static float Acos(float f) { return acosf(f); }
    static float Atan2(float y, float x) { return atan2f(y, x); }
};

/* -------------------------------------------------------------------------- */
/* Vector2                                                                    */
/* -------------------------------------------------------------------------- */

class Vector2 {
public:
    float x;
    float y;

    Vector2() { x = 0.0f; y = 0.0f; }
    Vector2(float x, float y) { this->x = x; this->y = y; }

    static Vector2 zero() {
        Vector2 r(0.0f, 0.0f);
        return r;
    }
    static Vector2 one() {
        Vector2 r(1.0f, 1.0f);
        return r;
    }
    static Vector2 up() {
        Vector2 r(0.0f, 1.0f);
        return r;
    }
    static Vector2 right() {
        Vector2 r(1.0f, 0.0f);
        return r;
    }

    float sqrMagnitude() { return x * x + y * y; }
    float magnitude() { return sqrtf(x * x + y * y); }

    Vector2 normalized() {
        float m = magnitude();
        Vector2 r;
        if (m > 1e-8f) {
            r.x = x / m;
            r.y = y / m;
        }
        return r;
    }

    static float Dot(const Vector2 &a, const Vector2 &b) {
        return a.x * b.x + a.y * b.y;
    }
    static float Distance(const Vector2 &a, const Vector2 &b) {
        float dx = a.x - b.x;
        float dy = a.y - b.y;
        return sqrtf(dx * dx + dy * dy);
    }
    static Vector2 Lerp(const Vector2 &a, const Vector2 &b, float t) {
        if (t < 0.0f) t = 0.0f;
        if (t > 1.0f) t = 1.0f;
        Vector2 r(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t);
        return r;
    }
    static Vector2 Scale(const Vector2 &a, const Vector2 &b) {
        Vector2 r(a.x * b.x, a.y * b.y);
        return r;
    }

    Vector2 operator+(const Vector2 &o) {
        Vector2 r(x + o.x, y + o.y);
        return r;
    }
    Vector2 operator-(const Vector2 &o) {
        Vector2 r(x - o.x, y - o.y);
        return r;
    }
    Vector2 scaled(float s) {
        Vector2 r(x * s, y * s);
        return r;
    }
};

/* -------------------------------------------------------------------------- */
/* Vector3                                                                    */
/* -------------------------------------------------------------------------- */

class Vector3 {
public:
    float x;
    float y;
    float z;

    Vector3() { x = 0.0f; y = 0.0f; z = 0.0f; }
    Vector3(float x, float y, float z) { this->x = x; this->y = y; this->z = z; }

    static Vector3 zero() {
        Vector3 r(0.0f, 0.0f, 0.0f);
        return r;
    }
    static Vector3 one() {
        Vector3 r(1.0f, 1.0f, 1.0f);
        return r;
    }
    static Vector3 up() {
        Vector3 r(0.0f, 1.0f, 0.0f);
        return r;
    }
    static Vector3 forward() {
        Vector3 r(0.0f, 0.0f, 1.0f);
        return r;
    }
    static Vector3 right() {
        Vector3 r(1.0f, 0.0f, 0.0f);
        return r;
    }

    float sqrMagnitude() { return x * x + y * y + z * z; }
    float magnitude() { return sqrtf(x * x + y * y + z * z); }

    Vector3 normalized() {
        float m = magnitude();
        Vector3 r;
        if (m > 1e-8f) {
            r.x = x / m;
            r.y = y / m;
            r.z = z / m;
        }
        return r;
    }

    static float Dot(const Vector3 &a, const Vector3 &b) {
        return a.x * b.x + a.y * b.y + a.z * b.z;
    }
    static Vector3 Cross(const Vector3 &a, const Vector3 &b) {
        Vector3 r(a.y * b.z - a.z * b.y,
                  a.z * b.x - a.x * b.z,
                  a.x * b.y - a.y * b.x);
        return r;
    }
    static float Distance(const Vector3 &a, const Vector3 &b) {
        float dx = a.x - b.x;
        float dy = a.y - b.y;
        float dz = a.z - b.z;
        return sqrtf(dx * dx + dy * dy + dz * dz);
    }
    static Vector3 Lerp(const Vector3 &a, const Vector3 &b, float t) {
        if (t < 0.0f) t = 0.0f;
        if (t > 1.0f) t = 1.0f;
        Vector3 r(a.x + (b.x - a.x) * t,
                  a.y + (b.y - a.y) * t,
                  a.z + (b.z - a.z) * t);
        return r;
    }
    static Vector3 Scale(const Vector3 &a, const Vector3 &b) {
        Vector3 r(a.x * b.x, a.y * b.y, a.z * b.z);
        return r;
    }

    Vector3 operator+(const Vector3 &o) {
        Vector3 r(x + o.x, y + o.y, z + o.z);
        return r;
    }
    Vector3 operator-(const Vector3 &o) {
        Vector3 r(x - o.x, y - o.y, z - o.z);
        return r;
    }
    Vector3 scaled(float s) {
        Vector3 r(x * s, y * s, z * s);
        return r;
    }
};

/* -------------------------------------------------------------------------- */
/* Quaternion                                                                 */
/* -------------------------------------------------------------------------- */

class Quaternion {
public:
    float x;
    float y;
    float z;
    float w;

    Quaternion() { x = 0.0f; y = 0.0f; z = 0.0f; w = 1.0f; }
    Quaternion(float x, float y, float z, float w) {
        this->x = x; this->y = y; this->z = z; this->w = w;
    }

    static Quaternion identity() {
        Quaternion r(0.0f, 0.0f, 0.0f, 1.0f);
        return r;
    }

    float sqrMagnitude() { return x * x + y * y + z * z + w * w; }
    float magnitude() { return sqrtf(x * x + y * y + z * z + w * w); }

    Quaternion normalized() {
        float m = magnitude();
        Quaternion r;
        if (m > 1e-8f) {
            r.x = x / m;
            r.y = y / m;
            r.z = z / m;
            r.w = w / m;
        } else {
            r.w = 1.0f;
        }
        return r;
    }

    /* Degrees. Same formula as Unity's Euler→quaternion. */
    static Quaternion Euler(float xDeg, float yDeg, float zDeg) {
        float hx = xDeg * (3.14159265358979323846f / 180.0f) * 0.5f;
        float hy = yDeg * (3.14159265358979323846f / 180.0f) * 0.5f;
        float hz = zDeg * (3.14159265358979323846f / 180.0f) * 0.5f;
        float cx = cosf(hx); float sx = sinf(hx);
        float cy = cosf(hy); float sy = sinf(hy);
        float cz = cosf(hz); float sz = sinf(hz);
        Quaternion r(
            sx * cy * cz + cx * sy * sz,
            cx * sy * cz - sx * cy * sz,
            cx * cy * sz - sx * sy * cz,
            cx * cy * cz + sx * sy * sz);
        return r;
    }

    static float Dot(const Quaternion &a, const Quaternion &b) {
        return a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w;
    }

    static Quaternion Normalize(const Quaternion &q) {
        Quaternion t = q;
        return t.normalized();
    }

    /* Hamilton product (Unity `q * p`). */
    Quaternion Multiply(const Quaternion &o) {
        Quaternion r(
            w * o.x + x * o.w + y * o.z - z * o.y,
            w * o.y - x * o.z + y * o.w + z * o.x,
            w * o.z + x * o.y - y * o.x + z * o.w,
            w * o.w - x * o.x - y * o.y - z * o.z);
        return r;
    }

    /* Rotate vector by this quaternion (Unity `q * v`). */
    Vector3 Rotate(const Vector3 &v) {
        Quaternion q = normalized();
        Quaternion p(v.x, v.y, v.z, 0.0f);
        Quaternion qi(-q.x, -q.y, -q.z, q.w);
        Quaternion qp = q.Multiply(p);
        Quaternion r = qp.Multiply(qi);
        Vector3 out(r.x, r.y, r.z);
        return out;
    }
};
#endif
