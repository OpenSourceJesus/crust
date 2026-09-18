using UnityEngine;

[Shared]
public class Player : MonoBehaviour {
    public int hp;
    public float speed;

    public void Update() {
        transform.position = new Vector2(
            transform.position.x + speed * Time.deltaTime,
            transform.position.y);
        hp = hp;
    }
}
