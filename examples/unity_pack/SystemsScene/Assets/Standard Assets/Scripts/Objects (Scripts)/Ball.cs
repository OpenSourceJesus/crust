using UnityEngine;

/* Physics: authored Rigidbody2D integrates under Physics2D.gravity. */
public class Ball : MonoBehaviour {
    public Rigidbody2D rb;

    void Start ()
    {
        // rb.linearVelocity = Vector2.right / 2;
    }
}
