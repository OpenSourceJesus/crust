using UnityEngine;

public class Player : MonoBehaviour {
    public int hp;
    public float speed;

    public void Update() {
        transform.position += new Vector2(speed * Time.deltaTime, 0);
        hp = hp;
    }
}
