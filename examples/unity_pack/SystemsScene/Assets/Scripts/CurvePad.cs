using UnityEngine;

/* AnimationCurve subset: sample the default bounce curve in data.c. */
public class CurvePad : MonoBehaviour {
    public float baseY;
    public float amp;

    public void Update() {
        transform.position = new Vector2(
            transform.position.x,
            baseY + AnimationCurve.Evaluate(Time.time) * amp);
    }
}
