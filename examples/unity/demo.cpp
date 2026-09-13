#include "unity_math.h"

/* Return 0 on success. Each check encodes a known Unity-shaped identity. */
int main(void) {
    int fails = 0;

    float clamp_v = Mathf::Clamp(2.0f, 0.0f, 1.0f);
    if (!Mathf::Approximately(clamp_v, 1.0f)) fails = fails + 1;

    float lerp_v = Mathf::Lerp(0.0f, 10.0f, 0.5f);
    if (!Mathf::Approximately(lerp_v, 5.0f)) fails = fails + 1;

    float abs_v = Mathf::Abs(-3.0f);
    if (!Mathf::Approximately(abs_v, 3.0f)) fails = fails + 1;

    Vector2 a = Vector2(3.0f, 4.0f);
    float am = a.magnitude();
    if (!Mathf::Approximately(am, 5.0f)) fails = fails + 1;

    Vector2 right2 = Vector2::right();
    float ad = Vector2::Dot(a, right2);
    if (!Mathf::Approximately(ad, 3.0f)) fails = fails + 1;

    Vector2 z2 = Vector2::zero();
    Vector2 o2 = Vector2::one();
    Vector2 mid = Vector2::Lerp(z2, o2, 0.5f);
    if (!Mathf::Approximately(mid.x, 0.5f)) fails = fails + 1;

    Vector3 i = Vector3::right();
    Vector3 j = Vector3::up();
    Vector3 k = Vector3::Cross(i, j);
    if (!Mathf::Approximately(k.z, 1.0f)) fails = fails + 1;

    float ij = Vector3::Dot(i, j);
    if (!Mathf::Approximately(ij, 0.0f)) fails = fails + 1;

    Vector3 len3 = Vector3(1.0f, 2.0f, 2.0f);
    float lm = len3.magnitude();
    if (!Mathf::Approximately(lm, 3.0f)) fails = fails + 1;

    /* 90 deg about Y: +X should go to approximately -Z. */
    Quaternion yaw = Quaternion::Euler(0.0f, 90.0f, 0.0f);
    Vector3 spun = yaw.Rotate(i);
    if (!Mathf::Approximately(spun.x, 0.0f)) fails = fails + 1;
    if (!Mathf::Approximately(spun.z, -1.0f)) fails = fails + 1;

    Quaternion idq = Quaternion::identity();
    float id = Quaternion::Dot(idq, idq);
    if (!Mathf::Approximately(id, 1.0f)) fails = fails + 1;

    return fails;
}
