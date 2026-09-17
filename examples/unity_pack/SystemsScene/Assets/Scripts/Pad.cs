using UnityEngine;
using UnityEngine.InputSystem;

[Shared]
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
		transform.position += new Vector2(move * speed * Time.deltaTime, 0);
		print(move);
	}
}
