using UnityEngine;

/* Animation subset: bob Y from Time.time + Mathf.Sin. */
[Shared]
public class Bouncer : MonoBehaviour {
    public float baseY;
    public float amp;
    public float speed;

    public void Update() {
        transform.position = new Vector2(
            transform.position.x,
            baseY + Mathf.Sin(Time.time * speed) * amp);
    }
}
