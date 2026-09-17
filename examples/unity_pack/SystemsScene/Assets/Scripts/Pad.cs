using UnityEngine;

/* Input Manager subset: host-fed Horizontal axis (GLFW arrows/WASD). */
public class Pad : MonoBehaviour {
    public float speed;

    public void Update() {
        transform.position += new Vector2(
            Input.GetAxis("Horizontal") * speed * Time.deltaTime, 0);
    }
}
