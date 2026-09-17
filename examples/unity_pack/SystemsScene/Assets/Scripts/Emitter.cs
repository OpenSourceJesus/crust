using UnityEngine;

/* Particles subset: emit from the packed transform each Update. */
public class Emitter : MonoBehaviour {
    public float rate;

    public void Update() {
        ParticleSystem.Emit(transform.position.x, transform.position.y);
    }
}
