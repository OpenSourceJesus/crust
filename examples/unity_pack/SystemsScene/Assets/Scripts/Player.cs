using Extensions;
using UnityEngine;
using UnityEngine.InputSystem;

public class Player : MonoBehaviour
{
    public Rigidbody2D rb;
	public float moveSpeed;
	public Transform graphicsTrs;
	float xSize = 1;
	[HideInInspector]
	public Vector2 multSize = new Vector2(1, 1);

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
		transform.position += new Vector3(move * moveSpeed * Time.deltaTime, 0);
		print(move);
		System.Console.WriteLine("" + Time.time);
		SpriteRenderer spriteRend = gameObject.AddComponent<SpriteRenderer>();
		System.Console.WriteLine(spriteRend);
	}
}
