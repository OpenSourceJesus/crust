// SPDX-License-Identifier: MIT
// The part of the UnityEngine API that managed code can use in a hybrid unity_pack build (--hybrid, --managed): script methods the packer cannot lower
// to C (or that --managed moves) run here, on DotNetAnywhere, from their own C# source.  Pure C#: nothing in this file talks to the packed engine.  What
// does (a class's fields, its transform, Time) is generated beside it by tools/unity/unity_pack_hybrid.py, from the same accessors the lowered C code uses.
//
// What is here is the part of UnityEngine that is plain math and plain data: the vector, quaternion, colour and rectangle types, and Mathf.  They follow
// Unity's own definitions (Euler angles are ZXY, `a * b` applies b first, Mathf.Repeat / DeltaAngle as Unity has them), so a method gives the same answer
// managed as lowered.  A member this shim does not have is a compile error for the class that uses it, and that class keeps its lowered C (or its stub,
// with the old CS8000 warning): a hybrid build never changes what a pack that worked did.
using System;

namespace System
{
    [AttributeUsage(AttributeTargets.All)] public sealed class SerializableAttribute : Attribute { }
    // (the DotNetAnywhere corlib has no ValueTuple: `(float, int) F()` in a project helper needs these to compile)
    public struct ValueTuple<T1, T2>
    {
        public T1 Item1; public T2 Item2;
        public ValueTuple(T1 item1, T2 item2) { Item1 = item1; Item2 = item2; }
    }
    public struct ValueTuple<T1, T2, T3>
    {
        public T1 Item1; public T2 Item2; public T3 Item3;
        public ValueTuple(T1 item1, T2 item2, T3 item3) { Item1 = item1; Item2 = item2; Item3 = item3; }
    }
    [AttributeUsage(AttributeTargets.All)] public sealed class NonSerializedAttribute : Attribute { }
}

namespace UnityEngine
{
    [AttributeUsage(AttributeTargets.All)] public sealed class SerializeFieldAttribute : Attribute { }
    [AttributeUsage(AttributeTargets.All)] public sealed class HideInInspectorAttribute : Attribute { }
    [AttributeUsage(AttributeTargets.All)] public sealed class HeaderAttribute : Attribute { public HeaderAttribute(string h) { } }
    [AttributeUsage(AttributeTargets.All)] public sealed class TooltipAttribute : Attribute { public TooltipAttribute(string t) { } }
    [AttributeUsage(AttributeTargets.All)] public sealed class RangeAttribute : Attribute { public RangeAttribute(float a, float b) { } }

    public static class Mathf
    {
        public const float PI = 3.14159265358979f;
        public const float Deg2Rad = PI / 180f;
        public const float Rad2Deg = 180f / PI;
        public const float Infinity = float.PositiveInfinity;
        public const float NegativeInfinity = float.NegativeInfinity;
        public static readonly float Epsilon = 1.17549435E-38f;
        public static float Abs(float f) { return f < 0f ? -f : f; }
        public static int Abs(int i) { return i < 0 ? -i : i; }
        public static float Sqrt(float f) { return (float)Math.Sqrt(f); }
        public static float Sin(float f) { return (float)Math.Sin(f); }
        public static float Cos(float f) { return (float)Math.Cos(f); }
        public static float Tan(float f) { return (float)Math.Tan(f); }
        public static float Asin(float f) { return (float)Math.Asin(f); }
        public static float Acos(float f) { return (float)Math.Acos(f); }
        public static float Atan(float f) { return (float)Math.Atan(f); }
        public static float Atan2(float y, float x) { return (float)Math.Atan2(y, x); }
        public static float Pow(float f, float p) { return (float)Math.Pow(f, p); }
        public static float Exp(float f) { return (float)Math.Exp(f); }
        public static float Log(float f) { return (float)Math.Log(f); }
        public static float Log(float f, float b) { return (float)(Math.Log(f) / Math.Log(b)); }
        public static float Log10(float f) { return (float)Math.Log10(f); }
        public static float Min(float a, float b) { return a < b ? a : b; }
        public static float Max(float a, float b) { return a > b ? a : b; }
        public static int Min(int a, int b) { return a < b ? a : b; }
        public static int Max(int a, int b) { return a > b ? a : b; }
        public static float Clamp(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }
        public static int Clamp(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }
        public static float Clamp01(float v) { return v < 0f ? 0f : (v > 1f ? 1f : v); }
        public static float Lerp(float a, float b, float t) { return a + (b - a) * Clamp01(t); }
        public static float LerpUnclamped(float a, float b, float t) { return a + (b - a) * t; }
        public static float InverseLerp(float a, float b, float v) { return a != b ? Clamp01((v - a) / (b - a)) : 0f; }
        public static float Floor(float f) { return (float)Math.Floor(f); }
        public static float Ceil(float f) { return (float)Math.Ceiling(f); }
        public static float Round(float f) { return (float)Math.Round(f); }
        public static int FloorToInt(float f) { return (int)Math.Floor(f); }
        public static int CeilToInt(float f) { return (int)Math.Ceiling(f); }
        public static int RoundToInt(float f) { return (int)Math.Round(f); }
        public static float Sign(float f) { return f >= 0f ? 1f : -1f; }
        public static bool Approximately(float a, float b) { return Abs(b - a) < Max(0.000001f * Max(Abs(a), Abs(b)), Epsilon * 8f); }
        public static float Repeat(float t, float length) { return Clamp(t - Floor(t / length) * length, 0f, length); }
        public static float PingPong(float t, float length) { t = Repeat(t, length * 2f); return length - Abs(t - length); }
        public static float DeltaAngle(float current, float target)
        {
            float delta = Repeat(target - current, 360f);
            if (delta > 180f) delta -= 360f;
            return delta;
        }
        public static float LerpAngle(float a, float b, float t)
        {
            float delta = Repeat(b - a, 360f);
            if (delta > 180f) delta -= 360f;
            return a + delta * Clamp01(t);
        }
        public static float MoveTowards(float current, float target, float maxDelta)
        {
            if (Abs(target - current) <= maxDelta) return target;
            return current + Sign(target - current) * maxDelta;
        }
        public static float MoveTowardsAngle(float current, float target, float maxDelta)
        {
            float delta = DeltaAngle(current, target);
            if (-maxDelta < delta && delta < maxDelta) return target;
            target = current + delta;
            return MoveTowards(current, target, maxDelta);
        }
        public static float SmoothStep(float from, float to, float t)
        {
            t = Clamp01(t);
            t = -2f * t * t * t + 3f * t * t;
            return to * t + from * (1f - t);
        }
    }

    public struct Vector2
    {
        public float x, y;
        public Vector2(float x, float y) { this.x = x; this.y = y; }
        public static Vector2 zero { get { return new Vector2(0f, 0f); } }
        public static Vector2 one { get { return new Vector2(1f, 1f); } }
        public static Vector2 up { get { return new Vector2(0f, 1f); } }
        public static Vector2 down { get { return new Vector2(0f, -1f); } }
        public static Vector2 left { get { return new Vector2(-1f, 0f); } }
        public static Vector2 right { get { return new Vector2(1f, 0f); } }
        public static Vector2 positiveInfinity { get { return new Vector2(float.PositiveInfinity, float.PositiveInfinity); } }
        public static Vector2 negativeInfinity { get { return new Vector2(float.NegativeInfinity, float.NegativeInfinity); } }
        public float this[int i]
        {
            get { if (i == 0) return x; if (i == 1) return y; throw new IndexOutOfRangeException(); }
            set { if (i == 0) x = value; else if (i == 1) y = value; else throw new IndexOutOfRangeException(); }
        }
        public void Set(float nx, float ny) { x = nx; y = ny; }
        public float magnitude { get { return (float)Math.Sqrt(x * x + y * y); } }
        public float sqrMagnitude { get { return x * x + y * y; } }
        public Vector2 normalized { get { float m = magnitude; return m > 1e-5f ? new Vector2(x / m, y / m) : new Vector2(0f, 0f); } }
        public void Normalize() { Vector2 n = normalized; x = n.x; y = n.y; }
        public void Scale(Vector2 s) { x *= s.x; y *= s.y; }
        public static Vector2 Scale(Vector2 a, Vector2 b) { return new Vector2(a.x * b.x, a.y * b.y); }
        public static float Dot(Vector2 a, Vector2 b) { return a.x * b.x + a.y * b.y; }
        public static float Distance(Vector2 a, Vector2 b) { return (a - b).magnitude; }
        public static Vector2 Perpendicular(Vector2 d) { return new Vector2(-d.y, d.x); }
        public static Vector2 Reflect(Vector2 inDir, Vector2 inNormal) { return inDir - inNormal * (2f * Dot(inNormal, inDir)); }
        public static Vector2 Lerp(Vector2 a, Vector2 b, float t) { t = Mathf.Clamp01(t); return new Vector2(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t); }
        public static Vector2 LerpUnclamped(Vector2 a, Vector2 b, float t) { return new Vector2(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t); }
        public static Vector2 MoveTowards(Vector2 current, Vector2 target, float maxDistanceDelta)
        {
            Vector2 to = target - current;
            float d = to.magnitude;
            if (d <= maxDistanceDelta || d < Mathf.Epsilon) return target;
            return current + to / d * maxDistanceDelta;
        }
        public static Vector2 ClampMagnitude(Vector2 v, float maxLength)
        {
            float sqr = v.sqrMagnitude;
            if (sqr > maxLength * maxLength) { float m = (float)Math.Sqrt(sqr); return v / m * maxLength; }
            return v;
        }
        public static float Angle(Vector2 from, Vector2 to)
        {
            float denom = (float)Math.Sqrt(from.sqrMagnitude * to.sqrMagnitude);
            if (denom < Mathf.Epsilon) return 0f;
            float dot = Mathf.Clamp(Dot(from, to) / denom, -1f, 1f);
            return (float)Math.Acos(dot) * Mathf.Rad2Deg;
        }
        public static float SignedAngle(Vector2 from, Vector2 to)
        {
            return Angle(from, to) * Mathf.Sign(from.x * to.y - from.y * to.x);
        }
        public static Vector2 Min(Vector2 a, Vector2 b) { return new Vector2(Mathf.Min(a.x, b.x), Mathf.Min(a.y, b.y)); }
        public static Vector2 Max(Vector2 a, Vector2 b) { return new Vector2(Mathf.Max(a.x, b.x), Mathf.Max(a.y, b.y)); }
        public static Vector2 operator +(Vector2 a, Vector2 b) { return new Vector2(a.x + b.x, a.y + b.y); }
        public static Vector2 operator -(Vector2 a, Vector2 b) { return new Vector2(a.x - b.x, a.y - b.y); }
        public static Vector2 operator -(Vector2 a) { return new Vector2(-a.x, -a.y); }
        public static Vector2 operator *(Vector2 a, float k) { return new Vector2(a.x * k, a.y * k); }
        public static Vector2 operator *(float k, Vector2 a) { return new Vector2(a.x * k, a.y * k); }
        public static Vector2 operator *(Vector2 a, Vector2 b) { return new Vector2(a.x * b.x, a.y * b.y); }
        public static Vector2 operator /(Vector2 a, float k) { return new Vector2(a.x / k, a.y / k); }
        public static Vector2 operator /(Vector2 a, Vector2 b) { return new Vector2(a.x / b.x, a.y / b.y); }
        public static implicit operator Vector3(Vector2 v) { return new Vector3(v.x, v.y, 0f); }
        public static bool operator ==(Vector2 a, Vector2 b) { return a.x == b.x && a.y == b.y; }
        public static bool operator !=(Vector2 a, Vector2 b) { return !(a.x == b.x && a.y == b.y); }
        public override bool Equals(object o) { return o is Vector2 && this == (Vector2)o; }
        public override int GetHashCode() { return x.GetHashCode() ^ (y.GetHashCode() << 1); }
    }

    public struct Vector3
    {
        public float x, y, z;
        public Vector3(float x, float y, float z) { this.x = x; this.y = y; this.z = z; }
        public Vector3(float x, float y) { this.x = x; this.y = y; this.z = 0f; }
        public static Vector3 zero { get { return new Vector3(0f, 0f, 0f); } }
        public static Vector3 one { get { return new Vector3(1f, 1f, 1f); } }
        public static Vector3 up { get { return new Vector3(0f, 1f, 0f); } }
        public static Vector3 down { get { return new Vector3(0f, -1f, 0f); } }
        public static Vector3 left { get { return new Vector3(-1f, 0f, 0f); } }
        public static Vector3 right { get { return new Vector3(1f, 0f, 0f); } }
        public static Vector3 forward { get { return new Vector3(0f, 0f, 1f); } }
        public static Vector3 back { get { return new Vector3(0f, 0f, -1f); } }
        public static Vector3 positiveInfinity { get { return new Vector3(float.PositiveInfinity, float.PositiveInfinity, float.PositiveInfinity); } }
        public static Vector3 negativeInfinity { get { return new Vector3(float.NegativeInfinity, float.NegativeInfinity, float.NegativeInfinity); } }
        public float this[int i]
        {
            get { if (i == 0) return x; if (i == 1) return y; if (i == 2) return z; throw new IndexOutOfRangeException(); }
            set { if (i == 0) x = value; else if (i == 1) y = value; else if (i == 2) z = value; else throw new IndexOutOfRangeException(); }
        }
        public void Set(float nx, float ny, float nz) { x = nx; y = ny; z = nz; }
        public float magnitude { get { return (float)Math.Sqrt(x * x + y * y + z * z); } }
        public float sqrMagnitude { get { return x * x + y * y + z * z; } }
        public Vector3 normalized { get { float m = magnitude; return m > 1e-5f ? new Vector3(x / m, y / m, z / m) : new Vector3(0f, 0f, 0f); } }
        public void Normalize() { Vector3 n = normalized; x = n.x; y = n.y; z = n.z; }
        public void Scale(Vector3 s) { x *= s.x; y *= s.y; z *= s.z; }
        public static Vector3 Scale(Vector3 a, Vector3 b) { return new Vector3(a.x * b.x, a.y * b.y, a.z * b.z); }
        public static float Dot(Vector3 a, Vector3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
        public static Vector3 Cross(Vector3 a, Vector3 b) { return new Vector3(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x); }
        public static float Distance(Vector3 a, Vector3 b) { return (a - b).magnitude; }
        public static Vector3 Reflect(Vector3 inDir, Vector3 inNormal) { return inDir - inNormal * (2f * Dot(inNormal, inDir)); }
        public static Vector3 Project(Vector3 v, Vector3 onNormal)
        {
            float sqr = Dot(onNormal, onNormal);
            if (sqr < Mathf.Epsilon) return zero;
            return onNormal * (Dot(v, onNormal) / sqr);
        }
        public static Vector3 ProjectOnPlane(Vector3 v, Vector3 planeNormal)
        {
            float sqr = Dot(planeNormal, planeNormal);
            if (sqr < Mathf.Epsilon) return v;
            return v - planeNormal * (Dot(v, planeNormal) / sqr);
        }
        public static Vector3 Lerp(Vector3 a, Vector3 b, float t) { t = Mathf.Clamp01(t); return new Vector3(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t); }
        public static Vector3 LerpUnclamped(Vector3 a, Vector3 b, float t) { return new Vector3(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t); }
        public static Vector3 MoveTowards(Vector3 current, Vector3 target, float maxDistanceDelta)
        {
            Vector3 to = target - current;
            float d = to.magnitude;
            if (d <= maxDistanceDelta || d < Mathf.Epsilon) return target;
            return current + to / d * maxDistanceDelta;
        }
        public static Vector3 ClampMagnitude(Vector3 v, float maxLength)
        {
            float sqr = v.sqrMagnitude;
            if (sqr > maxLength * maxLength) { float m = (float)Math.Sqrt(sqr); return v / m * maxLength; }
            return v;
        }
        public static float Angle(Vector3 from, Vector3 to)
        {
            float denom = (float)Math.Sqrt(from.sqrMagnitude * to.sqrMagnitude);
            if (denom < Mathf.Epsilon) return 0f;
            float dot = Mathf.Clamp(Dot(from, to) / denom, -1f, 1f);
            return (float)Math.Acos(dot) * Mathf.Rad2Deg;
        }
        public static float SignedAngle(Vector3 from, Vector3 to, Vector3 axis)
        {
            return Angle(from, to) * Mathf.Sign(Dot(axis, Cross(from, to)));
        }
        public static Vector3 Min(Vector3 a, Vector3 b) { return new Vector3(Mathf.Min(a.x, b.x), Mathf.Min(a.y, b.y), Mathf.Min(a.z, b.z)); }
        public static Vector3 Max(Vector3 a, Vector3 b) { return new Vector3(Mathf.Max(a.x, b.x), Mathf.Max(a.y, b.y), Mathf.Max(a.z, b.z)); }
        public static Vector3 operator +(Vector3 a, Vector3 b) { return new Vector3(a.x + b.x, a.y + b.y, a.z + b.z); }
        public static Vector3 operator -(Vector3 a, Vector3 b) { return new Vector3(a.x - b.x, a.y - b.y, a.z - b.z); }
        public static Vector3 operator -(Vector3 a) { return new Vector3(-a.x, -a.y, -a.z); }
        public static Vector3 operator *(Vector3 a, float k) { return new Vector3(a.x * k, a.y * k, a.z * k); }
        public static Vector3 operator *(float k, Vector3 a) { return new Vector3(a.x * k, a.y * k, a.z * k); }
        public static Vector3 operator /(Vector3 a, float k) { return new Vector3(a.x / k, a.y / k, a.z / k); }
        public static implicit operator Vector2(Vector3 v) { return new Vector2(v.x, v.y); }
        public static bool operator ==(Vector3 a, Vector3 b) { return a.x == b.x && a.y == b.y && a.z == b.z; }
        public static bool operator !=(Vector3 a, Vector3 b) { return !(a.x == b.x && a.y == b.y && a.z == b.z); }
        public override bool Equals(object o) { return o is Vector3 && this == (Vector3)o; }
        public override int GetHashCode() { return x.GetHashCode() ^ (y.GetHashCode() << 1) ^ (z.GetHashCode() << 2); }
    }

    public struct Vector4
    {
        public float x, y, z, w;
        public Vector4(float x, float y, float z, float w) { this.x = x; this.y = y; this.z = z; this.w = w; }
        public Vector4(float x, float y, float z) { this.x = x; this.y = y; this.z = z; this.w = 0f; }
        public Vector4(float x, float y) { this.x = x; this.y = y; this.z = 0f; this.w = 0f; }
        public static Vector4 zero { get { return new Vector4(0f, 0f, 0f, 0f); } }
        public static Vector4 one { get { return new Vector4(1f, 1f, 1f, 1f); } }
        public float this[int i]
        {
            get { if (i == 0) return x; if (i == 1) return y; if (i == 2) return z; if (i == 3) return w; throw new IndexOutOfRangeException(); }
            set { if (i == 0) x = value; else if (i == 1) y = value; else if (i == 2) z = value; else if (i == 3) w = value; else throw new IndexOutOfRangeException(); }
        }
        public float magnitude { get { return (float)Math.Sqrt(x * x + y * y + z * z + w * w); } }
        public float sqrMagnitude { get { return x * x + y * y + z * z + w * w; } }
        public Vector4 normalized { get { float m = magnitude; return m > 1e-5f ? new Vector4(x / m, y / m, z / m, w / m) : zero; } }
        public static float Dot(Vector4 a, Vector4 b) { return a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w; }
        public static Vector4 Lerp(Vector4 a, Vector4 b, float t) { t = Mathf.Clamp01(t); return new Vector4(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t, a.w + (b.w - a.w) * t); }
        public static Vector4 operator +(Vector4 a, Vector4 b) { return new Vector4(a.x + b.x, a.y + b.y, a.z + b.z, a.w + b.w); }
        public static Vector4 operator -(Vector4 a, Vector4 b) { return new Vector4(a.x - b.x, a.y - b.y, a.z - b.z, a.w - b.w); }
        public static Vector4 operator -(Vector4 a) { return new Vector4(-a.x, -a.y, -a.z, -a.w); }
        public static Vector4 operator *(Vector4 a, float k) { return new Vector4(a.x * k, a.y * k, a.z * k, a.w * k); }
        public static Vector4 operator *(float k, Vector4 a) { return new Vector4(a.x * k, a.y * k, a.z * k, a.w * k); }
        public static Vector4 operator /(Vector4 a, float k) { return new Vector4(a.x / k, a.y / k, a.z / k, a.w / k); }
        public static implicit operator Vector4(Vector3 v) { return new Vector4(v.x, v.y, v.z, 0f); }
        public static implicit operator Vector4(Vector2 v) { return new Vector4(v.x, v.y, 0f, 0f); }
        public static implicit operator Vector3(Vector4 v) { return new Vector3(v.x, v.y, v.z); }
        public static implicit operator Vector2(Vector4 v) { return new Vector2(v.x, v.y); }
        public static bool operator ==(Vector4 a, Vector4 b) { return a.x == b.x && a.y == b.y && a.z == b.z && a.w == b.w; }
        public static bool operator !=(Vector4 a, Vector4 b) { return !(a == b); }
        public override bool Equals(object o) { return o is Vector4 && this == (Vector4)o; }
        public override int GetHashCode() { return x.GetHashCode() ^ (y.GetHashCode() << 1) ^ (z.GetHashCode() << 2) ^ (w.GetHashCode() >> 1); }
    }

    public struct Vector2Int
    {
        public int x, y;
        public Vector2Int(int x, int y) { this.x = x; this.y = y; }
        public static Vector2Int zero { get { return new Vector2Int(0, 0); } }
        public static Vector2Int one { get { return new Vector2Int(1, 1); } }
        public static Vector2Int up { get { return new Vector2Int(0, 1); } }
        public static Vector2Int down { get { return new Vector2Int(0, -1); } }
        public static Vector2Int left { get { return new Vector2Int(-1, 0); } }
        public static Vector2Int right { get { return new Vector2Int(1, 0); } }
        public int this[int i]
        {
            get { if (i == 0) return x; if (i == 1) return y; throw new IndexOutOfRangeException(); }
            set { if (i == 0) x = value; else if (i == 1) y = value; else throw new IndexOutOfRangeException(); }
        }
        public void Set(int nx, int ny) { x = nx; y = ny; }
        public float magnitude { get { return (float)Math.Sqrt((double)(x * x + y * y)); } }
        public int sqrMagnitude { get { return x * x + y * y; } }
        public static float Distance(Vector2Int a, Vector2Int b) { return (a - b).magnitude; }
        public static Vector2Int Scale(Vector2Int a, Vector2Int b) { return new Vector2Int(a.x * b.x, a.y * b.y); }
        public void Scale(Vector2Int s) { x *= s.x; y *= s.y; }
        public static Vector2Int Min(Vector2Int a, Vector2Int b) { return new Vector2Int(Mathf.Min(a.x, b.x), Mathf.Min(a.y, b.y)); }
        public static Vector2Int Max(Vector2Int a, Vector2Int b) { return new Vector2Int(Mathf.Max(a.x, b.x), Mathf.Max(a.y, b.y)); }
        public static Vector2Int FloorToInt(Vector2 v) { return new Vector2Int(Mathf.FloorToInt(v.x), Mathf.FloorToInt(v.y)); }
        public static Vector2Int CeilToInt(Vector2 v) { return new Vector2Int(Mathf.CeilToInt(v.x), Mathf.CeilToInt(v.y)); }
        public static Vector2Int RoundToInt(Vector2 v) { return new Vector2Int(Mathf.RoundToInt(v.x), Mathf.RoundToInt(v.y)); }
        public static Vector2Int operator +(Vector2Int a, Vector2Int b) { return new Vector2Int(a.x + b.x, a.y + b.y); }
        public static Vector2Int operator -(Vector2Int a, Vector2Int b) { return new Vector2Int(a.x - b.x, a.y - b.y); }
        public static Vector2Int operator -(Vector2Int a) { return new Vector2Int(-a.x, -a.y); }
        public static Vector2Int operator *(Vector2Int a, Vector2Int b) { return new Vector2Int(a.x * b.x, a.y * b.y); }
        public static Vector2Int operator *(Vector2Int a, int k) { return new Vector2Int(a.x * k, a.y * k); }
        public static Vector2Int operator *(int k, Vector2Int a) { return new Vector2Int(a.x * k, a.y * k); }
        public static Vector2Int operator /(Vector2Int a, int k) { return new Vector2Int(a.x / k, a.y / k); }
        public static implicit operator Vector2(Vector2Int v) { return new Vector2(v.x, v.y); }
        public static explicit operator Vector3Int(Vector2Int v) { return new Vector3Int(v.x, v.y, 0); }
        public static bool operator ==(Vector2Int a, Vector2Int b) { return a.x == b.x && a.y == b.y; }
        public static bool operator !=(Vector2Int a, Vector2Int b) { return !(a.x == b.x && a.y == b.y); }
        public override bool Equals(object o) { return o is Vector2Int && this == (Vector2Int)o; }
        public override int GetHashCode() { return x.GetHashCode() ^ (y.GetHashCode() << 1); }
    }

    public struct Vector3Int
    {
        public int x, y, z;
        public Vector3Int(int x, int y, int z) { this.x = x; this.y = y; this.z = z; }
        public Vector3Int(int x, int y) { this.x = x; this.y = y; this.z = 0; }
        public static Vector3Int zero { get { return new Vector3Int(0, 0, 0); } }
        public static Vector3Int one { get { return new Vector3Int(1, 1, 1); } }
        public static Vector3Int up { get { return new Vector3Int(0, 1, 0); } }
        public static Vector3Int down { get { return new Vector3Int(0, -1, 0); } }
        public static Vector3Int left { get { return new Vector3Int(-1, 0, 0); } }
        public static Vector3Int right { get { return new Vector3Int(1, 0, 0); } }
        public static Vector3Int forward { get { return new Vector3Int(0, 0, 1); } }
        public static Vector3Int back { get { return new Vector3Int(0, 0, -1); } }
        public int this[int i]
        {
            get { if (i == 0) return x; if (i == 1) return y; if (i == 2) return z; throw new IndexOutOfRangeException(); }
            set { if (i == 0) x = value; else if (i == 1) y = value; else if (i == 2) z = value; else throw new IndexOutOfRangeException(); }
        }
        public void Set(int nx, int ny, int nz) { x = nx; y = ny; z = nz; }
        public float magnitude { get { return (float)Math.Sqrt((double)(x * x + y * y + z * z)); } }
        public int sqrMagnitude { get { return x * x + y * y + z * z; } }
        public static float Distance(Vector3Int a, Vector3Int b) { return (a - b).magnitude; }
        public static Vector3Int Scale(Vector3Int a, Vector3Int b) { return new Vector3Int(a.x * b.x, a.y * b.y, a.z * b.z); }
        public void Scale(Vector3Int s) { x *= s.x; y *= s.y; z *= s.z; }
        public static Vector3Int Min(Vector3Int a, Vector3Int b) { return new Vector3Int(Mathf.Min(a.x, b.x), Mathf.Min(a.y, b.y), Mathf.Min(a.z, b.z)); }
        public static Vector3Int Max(Vector3Int a, Vector3Int b) { return new Vector3Int(Mathf.Max(a.x, b.x), Mathf.Max(a.y, b.y), Mathf.Max(a.z, b.z)); }
        public static Vector3Int FloorToInt(Vector3 v) { return new Vector3Int(Mathf.FloorToInt(v.x), Mathf.FloorToInt(v.y), Mathf.FloorToInt(v.z)); }
        public static Vector3Int CeilToInt(Vector3 v) { return new Vector3Int(Mathf.CeilToInt(v.x), Mathf.CeilToInt(v.y), Mathf.CeilToInt(v.z)); }
        public static Vector3Int RoundToInt(Vector3 v) { return new Vector3Int(Mathf.RoundToInt(v.x), Mathf.RoundToInt(v.y), Mathf.RoundToInt(v.z)); }
        public static Vector3Int operator +(Vector3Int a, Vector3Int b) { return new Vector3Int(a.x + b.x, a.y + b.y, a.z + b.z); }
        public static Vector3Int operator -(Vector3Int a, Vector3Int b) { return new Vector3Int(a.x - b.x, a.y - b.y, a.z - b.z); }
        public static Vector3Int operator -(Vector3Int a) { return new Vector3Int(-a.x, -a.y, -a.z); }
        public static Vector3Int operator *(Vector3Int a, Vector3Int b) { return new Vector3Int(a.x * b.x, a.y * b.y, a.z * b.z); }
        public static Vector3Int operator *(Vector3Int a, int k) { return new Vector3Int(a.x * k, a.y * k, a.z * k); }
        public static Vector3Int operator *(int k, Vector3Int a) { return new Vector3Int(a.x * k, a.y * k, a.z * k); }
        public static Vector3Int operator /(Vector3Int a, int k) { return new Vector3Int(a.x / k, a.y / k, a.z / k); }
        public static implicit operator Vector3(Vector3Int v) { return new Vector3(v.x, v.y, v.z); }
        public static explicit operator Vector2Int(Vector3Int v) { return new Vector2Int(v.x, v.y); }
        public static bool operator ==(Vector3Int a, Vector3Int b) { return a.x == b.x && a.y == b.y && a.z == b.z; }
        public static bool operator !=(Vector3Int a, Vector3Int b) { return !(a.x == b.x && a.y == b.y && a.z == b.z); }
        public override bool Equals(object o) { return o is Vector3Int && this == (Vector3Int)o; }
        public override int GetHashCode() { return x.GetHashCode() ^ (y.GetHashCode() << 1) ^ (z.GetHashCode() << 2); }
    }

    /** A unit quaternion.  Unity's conventions: `a * b` applies b first, Euler angles are applied Z, then X, then Y, and eulerAngles is in [0, 360). */
    public struct Quaternion
    {
        public float x, y, z, w;
        public Quaternion(float x, float y, float z, float w) { this.x = x; this.y = y; this.z = z; this.w = w; }
        public static Quaternion identity { get { return new Quaternion(0f, 0f, 0f, 1f); } }
        public float this[int i]
        {
            get { if (i == 0) return x; if (i == 1) return y; if (i == 2) return z; if (i == 3) return w; throw new IndexOutOfRangeException(); }
            set { if (i == 0) x = value; else if (i == 1) y = value; else if (i == 2) z = value; else if (i == 3) w = value; else throw new IndexOutOfRangeException(); }
        }
        public void Set(float nx, float ny, float nz, float nw) { x = nx; y = ny; z = nz; w = nw; }

        public static Quaternion operator *(Quaternion a, Quaternion b)
        {
            return new Quaternion(
                a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y,
                a.w * b.y + a.y * b.w + a.z * b.x - a.x * b.z,
                a.w * b.z + a.z * b.w + a.x * b.y - a.y * b.x,
                a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z);
        }
        public static Vector3 operator *(Quaternion q, Vector3 p)
        {
            float x2 = q.x * 2f, y2 = q.y * 2f, z2 = q.z * 2f;
            float xx = q.x * x2, yy = q.y * y2, zz = q.z * z2;
            float xy = q.x * y2, xz = q.x * z2, yz = q.y * z2;
            float wx = q.w * x2, wy = q.w * y2, wz = q.w * z2;
            return new Vector3(
                (1f - (yy + zz)) * p.x + (xy - wz) * p.y + (xz + wy) * p.z,
                (xy + wz) * p.x + (1f - (xx + zz)) * p.y + (yz - wx) * p.z,
                (xz - wy) * p.x + (yz + wx) * p.y + (1f - (xx + yy)) * p.z);
        }
        public static bool operator ==(Quaternion a, Quaternion b) { return Dot(a, b) > 0.999999f; }
        public static bool operator !=(Quaternion a, Quaternion b) { return !(Dot(a, b) > 0.999999f); }
        public override bool Equals(object o) { return o is Quaternion && this == (Quaternion)o; }
        public override int GetHashCode() { return x.GetHashCode() ^ (y.GetHashCode() << 1) ^ (z.GetHashCode() << 2) ^ (w.GetHashCode() >> 1); }

        public static float Dot(Quaternion a, Quaternion b) { return a.x * b.x + a.y * b.y + a.z * b.z + a.w * b.w; }
        public static float Angle(Quaternion a, Quaternion b)
        {
            float d = Mathf.Min(Mathf.Abs(Dot(a, b)), 1f);
            return d > 1f - 0.000001f ? 0f : (float)Math.Acos(d) * 2f * Mathf.Rad2Deg;      // (Unity: the same rotation is exactly 0, not float noise)
        }
        public static Quaternion Normalize(Quaternion q)
        {
            float m = (float)Math.Sqrt(Dot(q, q));
            if (m < Mathf.Epsilon) return identity;
            return new Quaternion(q.x / m, q.y / m, q.z / m, q.w / m);
        }
        public void Normalize() { this = Normalize(this); }
        public Quaternion normalized { get { return Normalize(this); } }
        public static Quaternion Inverse(Quaternion q)
        {
            float n = Dot(q, q);
            if (n < Mathf.Epsilon) return identity;
            return new Quaternion(-q.x / n, -q.y / n, -q.z / n, q.w / n);
        }
        public static Quaternion AngleAxis(float angle, Vector3 axis)
        {
            Vector3 a = axis.normalized;
            float half = angle * Mathf.Deg2Rad * 0.5f;
            float s = (float)Math.Sin(half);
            return new Quaternion(a.x * s, a.y * s, a.z * s, (float)Math.Cos(half));
        }
        /** Unity's order: Z, then X, then Y (about the world axes). */
        public static Quaternion Euler(float x, float y, float z)
        {
            float hx = x * Mathf.Deg2Rad * 0.5f, hy = y * Mathf.Deg2Rad * 0.5f, hz = z * Mathf.Deg2Rad * 0.5f;
            Quaternion qx = new Quaternion((float)Math.Sin(hx), 0f, 0f, (float)Math.Cos(hx));
            Quaternion qy = new Quaternion(0f, (float)Math.Sin(hy), 0f, (float)Math.Cos(hy));
            Quaternion qz = new Quaternion(0f, 0f, (float)Math.Sin(hz), (float)Math.Cos(hz));
            return qy * qx * qz;
        }
        public static Quaternion Euler(Vector3 e) { return Euler(e.x, e.y, e.z); }
        public Vector3 eulerAngles
        {
            get
            {
                Quaternion q = Normalize(this);
                float m10 = 2f * (q.x * q.y + q.z * q.w), m11 = 1f - 2f * (q.x * q.x + q.z * q.z), m12 = 2f * (q.y * q.z - q.x * q.w);
                float m02 = 2f * (q.x * q.z + q.y * q.w), m22 = 1f - 2f * (q.x * q.x + q.y * q.y);
                float m20 = 2f * (q.x * q.z - q.y * q.w), m00 = 1f - 2f * (q.y * q.y + q.z * q.z);
                float ex, ey, ez;
                float s = -m12;
                if (s > 0.999999f) { ex = (float)Math.PI * 0.5f; ey = (float)Math.Atan2(-m20, m00); ez = 0f; }
                else if (s < -0.999999f) { ex = -(float)Math.PI * 0.5f; ey = (float)Math.Atan2(-m20, m00); ez = 0f; }
                else { ex = (float)Math.Asin(s); ey = (float)Math.Atan2(m02, m22); ez = (float)Math.Atan2(m10, m11); }
                return new Vector3(Wrap360(ex * Mathf.Rad2Deg), Wrap360(ey * Mathf.Rad2Deg), Wrap360(ez * Mathf.Rad2Deg));
            }
            set { this = Euler(value); }
        }
        static float Wrap360(float a)
        {
            if (a < 0f) a += 360f;
            else if (a >= 360f) a -= 360f;
            return a;
        }
        public static Quaternion LookRotation(Vector3 forward, Vector3 upwards)
        {
            if (forward.sqrMagnitude < 1e-12f) return identity;
            Vector3 f = forward.normalized;
            Vector3 r = Vector3.Cross(upwards, f);
            if (r.sqrMagnitude < 1e-12f)
            {
                // up parallel to forward: any perpendicular will do
                r = Vector3.Cross(Mathf.Abs(f.y) < 0.99f ? Vector3.up : Vector3.right, f);
            }
            r = r.normalized;
            Vector3 u = Vector3.Cross(f, r);
            float m00 = r.x, m01 = u.x, m02 = f.x, m10 = r.y, m11 = u.y, m12 = f.y, m20 = r.z, m21 = u.z, m22 = f.z;
            // the rotation matrix whose columns are right, up, forward, as a quaternion (column vectors: x = (m21 - m12) / 4w, ..)
            float tr = m00 + m11 + m22;
            if (tr > 0f)
            {
                float n = (float)Math.Sqrt(tr + 1f);
                float k = 0.5f / n;
                return new Quaternion((m21 - m12) * k, (m02 - m20) * k, (m10 - m01) * k, n * 0.5f);
            }
            if (m00 >= m11 && m00 >= m22)
            {
                float n = (float)Math.Sqrt(1f + m00 - m11 - m22);
                float k = 0.5f / n;
                return new Quaternion(0.5f * n, (m01 + m10) * k, (m02 + m20) * k, (m21 - m12) * k);
            }
            if (m11 > m22)
            {
                float n = (float)Math.Sqrt(1f + m11 - m00 - m22);
                float k = 0.5f / n;
                return new Quaternion((m10 + m01) * k, 0.5f * n, (m21 + m12) * k, (m02 - m20) * k);
            }
            {
                float n = (float)Math.Sqrt(1f + m22 - m00 - m11);
                float k = 0.5f / n;
                return new Quaternion((m02 + m20) * k, (m12 + m21) * k, 0.5f * n, (m10 - m01) * k);
            }
        }
        public static Quaternion LookRotation(Vector3 forward) { return LookRotation(forward, Vector3.up); }
        public static Quaternion FromToRotation(Vector3 from, Vector3 to)
        {
            Vector3 a = from.normalized, b = to.normalized;
            float d = Vector3.Dot(a, b);
            if (d >= 1f - 1e-6f) return identity;
            if (d <= -1f + 1e-6f)
            {
                Vector3 axis = Vector3.Cross(Vector3.right, a);
                if (axis.sqrMagnitude < 1e-6f) axis = Vector3.Cross(Vector3.up, a);
                return AngleAxis(180f, axis);
            }
            Vector3 c = Vector3.Cross(a, b);
            float s = (float)Math.Sqrt((1f + d) * 2f);
            float inv = 1f / s;
            return Normalize(new Quaternion(c.x * inv, c.y * inv, c.z * inv, s * 0.5f));
        }
        public static Quaternion Lerp(Quaternion a, Quaternion b, float t) { return LerpUnclamped(a, b, Mathf.Clamp01(t)); }
        public static Quaternion LerpUnclamped(Quaternion a, Quaternion b, float t)
        {
            if (Dot(a, b) < 0f) b = new Quaternion(-b.x, -b.y, -b.z, -b.w);
            return Normalize(new Quaternion(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t, a.w + (b.w - a.w) * t));
        }
        public static Quaternion Slerp(Quaternion a, Quaternion b, float t) { return SlerpUnclamped(a, b, Mathf.Clamp01(t)); }
        public static Quaternion SlerpUnclamped(Quaternion a, Quaternion b, float t)
        {
            float dot = Dot(a, b);
            if (dot < 0f) { b = new Quaternion(-b.x, -b.y, -b.z, -b.w); dot = -dot; }
            if (dot > 0.9995f) return LerpUnclamped(a, b, t);
            float theta0 = (float)Math.Acos(dot);
            float theta = theta0 * t;
            float sin0 = (float)Math.Sin(theta0);
            float s0 = (float)Math.Cos(theta) - dot * (float)Math.Sin(theta) / sin0;
            float s1 = (float)Math.Sin(theta) / sin0;
            return new Quaternion(a.x * s0 + b.x * s1, a.y * s0 + b.y * s1, a.z * s0 + b.z * s1, a.w * s0 + b.w * s1);
        }
        public static Quaternion RotateTowards(Quaternion from, Quaternion to, float maxDegreesDelta)
        {
            float angle = Angle(from, to);
            if (angle == 0f) return to;
            return SlerpUnclamped(from, to, Mathf.Min(1f, maxDegreesDelta / angle));
        }
    }

    public struct Color
    {
        public float r, g, b, a;
        public Color(float r, float g, float b, float a) { this.r = r; this.g = g; this.b = b; this.a = a; }
        public Color(float r, float g, float b) { this.r = r; this.g = g; this.b = b; this.a = 1f; }
        public static Color red { get { return new Color(1f, 0f, 0f, 1f); } }
        public static Color green { get { return new Color(0f, 1f, 0f, 1f); } }
        public static Color blue { get { return new Color(0f, 0f, 1f, 1f); } }
        public static Color white { get { return new Color(1f, 1f, 1f, 1f); } }
        public static Color black { get { return new Color(0f, 0f, 0f, 1f); } }
        public static Color yellow { get { return new Color(1f, 235f / 255f, 4f / 255f, 1f); } }
        public static Color cyan { get { return new Color(0f, 1f, 1f, 1f); } }
        public static Color magenta { get { return new Color(1f, 0f, 1f, 1f); } }
        public static Color gray { get { return new Color(0.5f, 0.5f, 0.5f, 1f); } }
        public static Color grey { get { return new Color(0.5f, 0.5f, 0.5f, 1f); } }
        public static Color clear { get { return new Color(0f, 0f, 0f, 0f); } }
        public float grayscale { get { return 0.299f * r + 0.587f * g + 0.114f * b; } }
        public float this[int i]
        {
            get { if (i == 0) return r; if (i == 1) return g; if (i == 2) return b; if (i == 3) return a; throw new IndexOutOfRangeException(); }
            set { if (i == 0) r = value; else if (i == 1) g = value; else if (i == 2) b = value; else if (i == 3) a = value; else throw new IndexOutOfRangeException(); }
        }
        public static Color Lerp(Color x, Color y, float t) { t = Mathf.Clamp01(t); return new Color(x.r + (y.r - x.r) * t, x.g + (y.g - x.g) * t, x.b + (y.b - x.b) * t, x.a + (y.a - x.a) * t); }
        public static Color LerpUnclamped(Color x, Color y, float t) { return new Color(x.r + (y.r - x.r) * t, x.g + (y.g - x.g) * t, x.b + (y.b - x.b) * t, x.a + (y.a - x.a) * t); }
        public static Color operator +(Color p, Color q) { return new Color(p.r + q.r, p.g + q.g, p.b + q.b, p.a + q.a); }
        public static Color operator -(Color p, Color q) { return new Color(p.r - q.r, p.g - q.g, p.b - q.b, p.a - q.a); }
        public static Color operator *(Color p, Color q) { return new Color(p.r * q.r, p.g * q.g, p.b * q.b, p.a * q.a); }
        public static Color operator *(Color p, float k) { return new Color(p.r * k, p.g * k, p.b * k, p.a * k); }
        public static Color operator *(float k, Color p) { return new Color(p.r * k, p.g * k, p.b * k, p.a * k); }
        public static Color operator /(Color p, float k) { return new Color(p.r / k, p.g / k, p.b / k, p.a / k); }
        public static implicit operator Vector4(Color c) { return new Vector4(c.r, c.g, c.b, c.a); }
        public static implicit operator Color(Vector4 v) { return new Color(v.x, v.y, v.z, v.w); }
        public static bool operator ==(Color p, Color q) { return p.r == q.r && p.g == q.g && p.b == q.b && p.a == q.a; }
        public static bool operator !=(Color p, Color q) { return !(p == q); }
        public override bool Equals(object o) { return o is Color && this == (Color)o; }
        public override int GetHashCode() { return r.GetHashCode() ^ (g.GetHashCode() << 2) ^ (b.GetHashCode() >> 2) ^ (a.GetHashCode() >> 1); }
    }

    public struct Rect
    {
        public float x, y, width, height;
        public Rect(float x, float y, float width, float height) { this.x = x; this.y = y; this.width = width; this.height = height; }
        public Rect(Vector2 position, Vector2 size) { this.x = position.x; this.y = position.y; this.width = size.x; this.height = size.y; }
        public static Rect MinMaxRect(float xmin, float ymin, float xmax, float ymax) { return new Rect(xmin, ymin, xmax - xmin, ymax - ymin); }
        public Vector2 position { get { return new Vector2(x, y); } set { x = value.x; y = value.y; } }
        public Vector2 size { get { return new Vector2(width, height); } set { width = value.x; height = value.y; } }
        public Vector2 center { get { return new Vector2(x + width / 2f, y + height / 2f); } set { x = value.x - width / 2f; y = value.y - height / 2f; } }
        public Vector2 min { get { return new Vector2(xMin, yMin); } set { xMin = value.x; yMin = value.y; } }
        public Vector2 max { get { return new Vector2(xMax, yMax); } set { xMax = value.x; yMax = value.y; } }
        public float xMin { get { return x; } set { float xm = xMax; x = value; width = xm - x; } }
        public float yMin { get { return y; } set { float ym = yMax; y = value; height = ym - y; } }
        public float xMax { get { return width + x; } set { width = value - x; } }
        public float yMax { get { return height + y; } set { height = value - y; } }
        public bool Contains(Vector2 point) { return point.x >= xMin && point.x < xMax && point.y >= yMin && point.y < yMax; }
        public bool Overlaps(Rect o) { return o.xMax > xMin && o.xMin < xMax && o.yMax > yMin && o.yMin < yMax; }
        public static bool operator ==(Rect a, Rect b) { return a.x == b.x && a.y == b.y && a.width == b.width && a.height == b.height; }
        public static bool operator !=(Rect a, Rect b) { return !(a == b); }
        public override bool Equals(object o) { return o is Rect && this == (Rect)o; }
        public override int GetHashCode() { return x.GetHashCode() ^ (width.GetHashCode() << 2) ^ (y.GetHashCode() >> 2) ^ (height.GetHashCode() >> 1); }
    }

    public struct Bounds
    {
        Vector3 m_Center, m_Extents;
        public Bounds(Vector3 center, Vector3 size) { m_Center = center; m_Extents = size * 0.5f; }
        public Vector3 center { get { return m_Center; } set { m_Center = value; } }
        public Vector3 size { get { return m_Extents * 2f; } set { m_Extents = value * 0.5f; } }
        public Vector3 extents { get { return m_Extents; } set { m_Extents = value; } }
        public Vector3 min { get { return m_Center - m_Extents; } set { SetMinMax(value, max); } }
        public Vector3 max { get { return m_Center + m_Extents; } set { SetMinMax(min, value); } }
        public void SetMinMax(Vector3 mn, Vector3 mx) { m_Extents = (mx - mn) * 0.5f; m_Center = mn + m_Extents; }
        public void Encapsulate(Vector3 point) { SetMinMax(Vector3.Min(min, point), Vector3.Max(max, point)); }
        public void Encapsulate(Bounds b) { Encapsulate(b.center - b.extents); Encapsulate(b.center + b.extents); }
        public void Expand(float amount) { amount *= 0.5f; m_Extents = m_Extents + new Vector3(amount, amount, amount); }
        public bool Contains(Vector3 p)
        {
            Vector3 mn = min, mx = max;
            return p.x >= mn.x && p.x <= mx.x && p.y >= mn.y && p.y <= mx.y && p.z >= mn.z && p.z <= mx.z;
        }
        public bool Intersects(Bounds b)
        {
            Vector3 amn = min, amx = max, bmn = b.min, bmx = b.max;
            return amn.x <= bmx.x && amx.x >= bmn.x && amn.y <= bmx.y && amx.y >= bmn.y && amn.z <= bmx.z && amx.z >= bmn.z;
        }
        public static bool operator ==(Bounds a, Bounds b) { return a.m_Center == b.m_Center && a.m_Extents == b.m_Extents; }
        public static bool operator !=(Bounds a, Bounds b) { return !(a == b); }
        public override bool Equals(object o) { return o is Bounds && this == (Bounds)o; }
        public override int GetHashCode() { return m_Center.GetHashCode() ^ (m_Extents.GetHashCode() << 2); }
    }

    public static class Debug
    {
        public static void Log(object o) { Console.WriteLine(o == null ? "null" : o.ToString()); }
        public static void LogWarning(object o) { Console.WriteLine(o == null ? "null" : o.ToString()); }
        public static void LogError(object o) { Console.WriteLine(o == null ? "null" : o.ToString()); }
    }

    public enum Space { World, Self }

    /** The transform of the object a script belongs to.  Which engine arrays it reads is the packer's to say, so each hybrid class has a subclass (generated),
        which gives `position` and `rotation`; everything else here is plain C# over those two.  Like the packed engine, a transform is its own world: no parent
        (local and world rotation are one). */
    public abstract class Transform
    {
        public abstract Vector3 position { get; set; }
        public abstract Quaternion rotation { get; set; }
        /** the position in the parent's frame (the packed engine stores it so); `position` is the world one, composed through the parents */
        public abstract Vector3 localPosition { get; set; }
        /** null for no parent; otherwise a handle that only says there is one (what the packed engine can say of it): `transform.parent == null`,
            `SetParent(null)`.  A hybrid class whose code uses either is declined at pack time when the engine has not got the functions. */
        public virtual TransformRef parent { get { throw new System.NotSupportedException("the packed engine has no parent for this class"); } }
        public virtual void SetParent(TransformRef p, bool worldPositionStays) { throw new System.NotSupportedException("the packed engine has no SetParent for this class"); }
        public void SetParent(TransformRef p) { SetParent(p, true); }
        public Quaternion localRotation { get { return rotation; } set { rotation = value; } }
        public Vector3 eulerAngles { get { return rotation.eulerAngles; } set { rotation = Quaternion.Euler(value); } }
        public Vector3 localEulerAngles { get { return eulerAngles; } set { eulerAngles = value; } }
        public Vector3 right { get { return rotation * Vector3.right; } }
        public Vector3 up { get { return rotation * Vector3.up; } }
        public Vector3 forward { get { return rotation * Vector3.forward; } }
        public Vector3 TransformDirection(Vector3 direction) { return rotation * direction; }
        public Vector3 InverseTransformDirection(Vector3 direction) { return Quaternion.Inverse(rotation) * direction; }
        /** Self: the rotation is applied in the object's own frame (`rotation * e`); World: about the world axes (`e * rotation`). */
        public void Rotate(Vector3 eulers, Space relativeTo)
        {
            Quaternion e = Quaternion.Euler(eulers.x, eulers.y, eulers.z);
            rotation = relativeTo == Space.Self ? Quaternion.Normalize(rotation * e) : Quaternion.Normalize(e * rotation);
        }
        public void Rotate(Vector3 eulers) { Rotate(eulers, Space.Self); }
        public void Rotate(float xAngle, float yAngle, float zAngle, Space relativeTo) { Rotate(new Vector3(xAngle, yAngle, zAngle), relativeTo); }
        public void Rotate(float xAngle, float yAngle, float zAngle) { Rotate(new Vector3(xAngle, yAngle, zAngle), Space.Self); }
        public void Rotate(Vector3 axis, float angle, Space relativeTo)
        {
            Quaternion r = Quaternion.AngleAxis(angle, axis);
            rotation = relativeTo == Space.Self ? Quaternion.Normalize(rotation * r) : Quaternion.Normalize(r * rotation);
        }
        public void Rotate(Vector3 axis, float angle) { Rotate(axis, angle, Space.Self); }
        public void LookAt(Vector3 worldPosition, Vector3 worldUp) { rotation = Quaternion.LookRotation(worldPosition - position, worldUp); }
        public void LookAt(Vector3 worldPosition) { LookAt(worldPosition, Vector3.up); }
        public void LookAt(Transform target, Vector3 worldUp) { LookAt(target.position, worldUp); }
        public void LookAt(Transform target) { LookAt(target.position, Vector3.up); }
    }

    public class TransformRef { }

    public class MonoBehaviour
    {
        /** the object's index in its class's packed arrays: the `i` of every `Class_method(i, ..)` in engine.c */
        public uint __i;
        public Transform transform;
        /** the component of a script class's name on this object, as the hybrid class generated for it (null for none); generated classes override it
            for the components the packed engine can find (GetComponent<T>() below goes through it) */
        public virtual object __component(string typeName) { return null; }
        public T GetComponent<T>() where T : class { return __component(typeof(T).Name) as T; }
    }
}
