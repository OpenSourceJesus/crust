using UnityEngine;
using UnityEngine.InputSystem;

/* Input Manager subset: host-fed Horizontal axis. */
[Shared]
public class Pad : MonoBehaviour
{
	public float speed;

	public void Update ()
	{
		float move = 0;
		if (Keyboard.current.leftArrowKey.isPressed)
			move --;
		if (Keyboard.current.rightArrowKey.isPressed)
			move ++;
		transform.position += new Vector2(move * speed * Time.deltaTime, 0);
	}
}
