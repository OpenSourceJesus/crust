using UnityEngine;
using UnityEngine.InputSystem;

public class Pad : MonoBehaviour
{
	public float speed;

	void Start ()
	{
		Debug.Log("Hello World!");
		System.Console.WriteLine(GameObject.Find("BouncePad").GetComponent<Bouncer>().amp);
	}

	void Update ()
	{
		float move = 0;
		if (Keyboard.current.leftArrowKey.isPressed)
			move --;
		if (Keyboard.current.rightArrowKey.isPressed)
			move ++;
		transform.position = new Vector2(
			transform.position.x + move * speed * Time.deltaTime,
			transform.position.y);
		print(move);
		System.Console.WriteLine("" + Time.time);
		SpriteRenderer spriteRend = gameObject.AddComponent<SpriteRenderer>();
		System.Console.WriteLine(spriteRend);
	}
}
