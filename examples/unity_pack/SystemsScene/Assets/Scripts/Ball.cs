using UnityEngine;

/* Physics subset: FixedUpdate + Physics2D.gravity on float velocity. */
public class Ball : MonoBehaviour {
    public float velX;
    public float velY;
    public float gravityScale;

    public void FixedUpdate() {
        velY = velY + Physics2D.gravity.y * gravityScale * Time.fixedDeltaTime;
        transform.position += new Vector2(
            velX * Time.fixedDeltaTime,
            velY * Time.fixedDeltaTime);
    }
}
