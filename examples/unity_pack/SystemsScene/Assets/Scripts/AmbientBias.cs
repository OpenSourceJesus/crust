using UnityEngine;

/* Lighting subset: read authored ambient; no invented Light. */
public class AmbientBias : MonoBehaviour {
    public float lift;

    public void Update() {
        lift = RenderSettings.ambientLight.r;
        transform.position = new Vector2(
            transform.position.x,
            transform.position.y + lift * 0.f);
    }
}
